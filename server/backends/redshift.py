"""
backends/redshift.py

RedshiftBackend: talks to Amazon Redshift via psycopg2 - the same driver
backends/postgres.py uses. Redshift's wire protocol is Postgres-compatible
for ordinary SQL/DDL/DML, so no new client library is needed (unlike every
other non-Postgres/MySQL dialect added so far - BigQuery, Snowflake,
Databricks, Oracle - which each needed their own driver).

Mirrors backends/oracle.py's shape more than backends/postgres.py's,
though: a Redshift connection has multiple identifying fields (host, port,
database, user) rather than Postgres's single connection-string form, so
every connection - preset or custom - needs its own explicit descriptor,
the same "structured descriptor" pattern Oracle/Databricks/Snowflake use.

A Redshift descriptor looks like:
    {"type": "redshift", "host": "...", "port": 5439, "database": "...",
     "user": "...", "password": "...", "schema": "..."}
"host"/"database"/"user"/"password" are required; "port" defaults to
Redshift's standard port (5439) when omitted. "schema" is optional -
Redshift has genuine Postgres-style schemas (unlike Oracle, where a
"schema" is really a same-named user) - given, connect() below runs `SET
search_path TO <schema>, public` right after connecting, the same
"optional namespace override" role Oracle's/Snowflake's/Databricks' own
"schema" descriptor field plays; omitted, the connecting user's own default
search_path applies as normal.

This first pass is deliberately narrower than Redshift is capable of,
mirroring how Oracle's/Databricks' first passes were narrowed too:
- Only plain host/port/database + username/password authentication is
  supported. AWS IAM temporary credentials (boto3's
  redshift.get_cluster_credentials, avoiding a stored static password) and
  the Redshift Data API (an entirely different async submit-and-poll
  execution model, useful for Serverless/network-isolated clusters this
  app can't reach directly) are both deferred follow-up work, not built
  into this first pass - see REDSHIFT_SCOPE.md.
- TLS is NOT optional/opt-in the way it is for Oracle Cloud - connect()
  below always passes sslmode="require". There's no legitimate on-prem/
  no-TLS Redshift deployment the way there is for Oracle XE, and Redshift's
  own docs recommend requiring SSL for any connection reaching a cluster
  over the public internet - so unlike Oracle's "ssl" descriptor field,
  there's deliberately no equivalent opt-out flag here.

Which of "password" must never round-trip back to the frontend once saved
is state_store.py's _CREDENTIAL_CONFIG_FIELDS' responsibility - "password"
is already covered there (shared with Oracle's own standalone "password"
field), no new field name needed.

Schema introspection reuses backends/postgres.py's information_schema
queries where Redshift's own catalog genuinely matches Postgres's (columns,
constraints, views), but does NOT copy two sections that don't apply here:
  - No "Indexes" section. Redshift has no CREATE INDEX / B-tree index
    concept at all - physical layout is instead controlled by DISTKEY (how
    rows are distributed across compute nodes) and SORTKEY (how rows are
    ordered on disk), which matter far more to Redshift query performance
    than an index list would and are worth surfacing to Gemini instead -
    see the "Distribution/Sort Keys" section below, sourced from Redshift's
    own SVV_TABLE_INFO system view (which conveniently pre-formats
    DISTSTYLE as a readable string, e.g. "KEY(customer_id)"/"EVEN"/"ALL"/
    "AUTO(ALL)", rather than needing to reconstruct it from lower-level
    catalog tables).
  - No "Triggers" section. Redshift has zero trigger support - there's
    nothing meaningful to query, so the section is omitted entirely rather
    than issuing a query against a catalog view that may not even exist.
  - Constraints (PK/FK/UNIQUE) ARE included, same query shape as Postgres,
    but Redshift - like Snowflake/Databricks - lets you declare them
    without ever enforcing them at write time; the schema text says so
    explicitly, and translate_routes.py's dialect prompt intro repeats the
    same warning, so generated SQL doesn't assume DB-enforced integrity.
  - Grants: deliberately omitted. information_schema.role_table_grants
    support is inconsistent across Redshift versions/configurations
    (svv_relation_privileges may be the more portable source) - left for
    follow-up rather than risking an unverified query.

Scoped with current_schema() rather than a hardcoded 'public' the way
backends/postgres.py hardcodes it - this dialect explicitly supports a
"schema" descriptor override (see connect() above), so introspection needs
to respect whatever search_path that override left in effect, not assume
'public' unconditionally the way Postgres's simpler (no schema-override
field) descriptor can get away with.

NOTE for reviewers: like backends/snowflake.py/backends/databricks.py/
backends/oracle.py, this has been exercised against the fake DB-API harness
in tests/server/helpers.py (reusing the existing Postgres psycopg2 fake,
since the driver is identical), not a real Redshift cluster yet - treat the
SVV_TABLE_INFO query and the "grants omitted" call above as solid first
drafts to validate before relying on them against a real cluster.
"""

import psycopg2
from psycopg2 import sql
import sqlparse

from .base import (
    Backend, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
)


class RedshiftBackend(Backend):
    dialect_name = "Amazon Redshift SQL"

    # Redshift supports a bare "SELECT 1" (Postgres-derived, no FROM
    # required) - the base class's default is correct as-is, no override
    # needed (unlike Oracle's).
    liveness_sql = "SELECT 1"

    def connect(self, descriptor):
        descriptor = descriptor or {}
        host = descriptor.get("host") or ""
        port = descriptor.get("port") or 5439
        database = descriptor.get("database") or ""
        user = descriptor.get("user") or ""
        password = descriptor.get("password") or ""
        schema = descriptor.get("schema") or None

        if not host:
            raise ValueError("Redshift connection requires a host - none was provided.")
        if not database:
            raise ValueError("Redshift connection requires a database - none was provided.")
        if not (user and password):
            raise ValueError("Redshift connection requires a user and password - one was missing.")

        # sslmode="require" is always passed, not an opt-in flag - see the
        # module docstring above for why Redshift gets no Oracle-style
        # opt-out. connect_timeout bounds only TCP/handshake setup, never
        # query execution afterwards - see backends/base.py's
        # DB_CONNECT_TIMEOUT_SECONDS docstring (this is the exact dialect/
        # failure mode - a Redshift Serverless workgroup with a closed
        # security group or bad DNS record - that motivated adding it).
        connection = psycopg2.connect(
            host=host, port=port, dbname=database, user=user, password=password,
            sslmode="require", connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        )
        # Set once up front (rather than only inside execute() the way
        # Postgres/Oracle do it) so the SET search_path statement right
        # below doesn't need its own explicit commit() call.
        connection.autocommit = True

        if schema:
            # psycopg2.sql.Identifier does correct, driver-native
            # quoting/escaping for an interpolated identifier - SET has no
            # parameterized form (same limitation Oracle's ALTER SESSION SET
            # CURRENT_SCHEMA has), but unlike backends/oracle.py's own
            # hand-rolled identifier regex, psycopg2 already ships a safe,
            # correct way to do this, so there's no need to reinvent one
            # here.
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
                )

        return connection

    def close(self, connection):
        if connection:
            connection.close()

    def cache_key(self, descriptor):
        """host:port/database.schema, parsed straight from the descriptor -
        never a credential. Same non-sensitive-identifier role
        OracleBackend's/SnowflakeBackend's/DatabricksBackend's cache_key
        plays."""
        descriptor = descriptor or {}
        host = descriptor.get("host") or "unknown"
        port = descriptor.get("port") or "unknown"
        database = descriptor.get("database") or "unknown"
        schema = descriptor.get("schema") or "public"
        return f"{host}:{port}/{database}.{schema}"

    def identity_label(self, connection):
        db_name, username = "Unknown", "Unknown"
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user;")
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
            # date-shard families and capped to SCHEMA_MAX_TABLES entries
            # (see backends/base.py) before any column/constraint/layout/
            # view query runs, same staging as every other backend's
            # get_schema(). Scoped to current_schema() rather than a
            # hardcoded 'public' - see module docstring above.
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
            # No native wildcard-table query mechanism (unlike BigQuery), so
            # a shard family's representative is described under its own
            # real, literal name - mirrors every other backend here.
            shard_by_representative = {
                members[-1]: (prefix, members) for prefix, members in shard_groups.items()
            }

            # 1. Tables and columns - scoped to the bounded kept_names set.
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
                default_str = f" DEFAULT {col_default}" if col_default else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                tables.setdefault(table_name, []).append(
                    f"  {col_name} {data_type} {null_str}{default_str}"
                )

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

            # 2. Constraints - declared but never enforced by Redshift at
            # write time (same caveat as Snowflake/Databricks - see module
            # docstring). Best-effort: a role without catalog access, or a
            # cluster/version where one of these information_schema views
            # behaves unexpectedly, degrades to "skip this section" rather
            # than a failed schema fetch (mirrors backends/oracle.py's own
            # try/except around its constraints/views queries).
            try:
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
                    lines = []
                    for tbl, c_name, c_type, col, f_tbl, f_col in constraints:
                        if c_type == 'FOREIGN KEY':
                            lines.append(f"  [{tbl}] {c_name} ({c_type}): {col} -> {f_tbl}({f_col})")
                        elif col:
                            lines.append(f"  [{tbl}] {c_name} ({c_type}): {col}")
                        else:
                            lines.append(f"  [{tbl}] {c_name} ({c_type})")
                    schema_parts.append(
                        "Constraints (declared only - Redshift never enforces these at write "
                        "time):\n" + "\n".join(lines)
                    )
            except Exception:
                pass

            # 3. Distribution/Sort keys - Redshift's replacement for an
            # index list (see module docstring for why there's no separate
            # "Indexes" section). SVV_TABLE_INFO conveniently pre-formats
            # DISTSTYLE as a readable string (e.g. "KEY(customer_id)",
            # "EVEN", "ALL", "AUTO(ALL)") rather than needing it
            # reconstructed from lower-level catalog tables. Best-effort,
            # same reasoning as the constraints query above.
            try:
                cursor.execute("""
                    SELECT "table", diststyle, sortkey1
                    FROM svv_table_info
                    WHERE schema = current_schema()
                      AND "table" = ANY(%s);
                """, (kept_names,))
                layout_rows = cursor.fetchall()
                lines = []
                for tbl, diststyle, sortkey1 in layout_rows:
                    parts = []
                    if diststyle:
                        parts.append(f"DISTSTYLE {diststyle}")
                    if sortkey1:
                        parts.append(f"SORTKEY({sortkey1})")
                    if parts:
                        lines.append(f"  [{tbl}] {', '.join(parts)}")
                if lines:
                    schema_parts.append(
                        "Distribution/Sort Keys (Redshift has no index concept - this is what "
                        "actually drives query performance, not an index list):\n"
                        + "\n".join(lines)
                    )
            except Exception:
                pass

            # 4. Views - deliberately NOT scoped to kept_names, same
            # reasoning as every other backend here: that set is built
            # exclusively from BASE TABLE names, so no view name could ever
            # appear in it.
            cursor.execute("""
                SELECT
                    table_name,
                    view_definition
                FROM information_schema.views
                WHERE table_schema = current_schema();
            """)
            views = cursor.fetchall()
            if views:
                view_lines = [f"  View {v[0]}: {(v[1] or '').strip()}" for v in views]
                schema_parts.append("Views:\n" + "\n".join(view_lines))

            # Deliberately no Indexes/Triggers/Grants sections - see module
            # docstring for why (no such concept for the first two; the
            # third left for follow-up, unverified against a real cluster).

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
