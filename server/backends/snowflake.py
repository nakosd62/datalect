"""
backends/snowflake.py

SnowflakeBackend: talks to Snowflake via snowflake-connector-python (the
official DB-API driver). Mirrors PostgresBackend's shape more than
BigQueryBackend's for get_schema()/execute() - Snowflake's
INFORMATION_SCHEMA and cursor/DB-API behavior are much closer to Postgres's
than to BigQuery's job-based client - but mirrors BigQueryBackend's
connect()/descriptor shape, since Snowflake, like BigQuery and unlike
Postgres, has no single connection-string form and needs a structured,
credential-bearing descriptor instead.

A Snowflake descriptor looks like:
    {"type": "snowflake", "account": "...", "user": "...",
     "warehouse": "...", "database": "...", "schema": "...", "role": "...",
     "password": "..."}
or, for key-pair auth instead of a password:
    {"type": "snowflake", "account": "...", "user": "...",
     "warehouse": "...", "database": "...", "schema": "...", "role": "...",
     "private_key": "<PEM text>", "private_key_passphrase": "..."}

"account"/"user"/"warehouse"/"database" are required - Snowflake has no
ambient-identity mode the way BigQuery's Application Default Credentials
does (see backends/bigquery.py's module docstring), so every connection,
preset or custom, must carry a real, explicit credential. "schema" and
"role" are optional: omitted, Snowflake falls back to the user's default
schema/role for the account. Exactly one of "password"/"private_key" must
be supplied - connect() raises if neither is, rather than letting the
connector fail with a less obvious error. "private_key_passphrase" is only
meaningful alongside "private_key", for a key that was itself encrypted at
generation time.

Which of "password" / "private_key" / "private_key_passphrase" must never
round-trip back to the frontend once saved is state_store.py's
_CREDENTIAL_CONFIG_FIELDS' responsibility, mirrored from how it already
handles bigquery.py's "credentials_json" - see that module's docstring.

NOTE for reviewers: this module's get_schema()/execute()/identity_label()
queries are written from Snowflake's documented INFORMATION_SCHEMA/session-
function behavior (CURRENT_SCHEMA(), CURRENT_DATABASE(), CURRENT_USER(),
information_schema.{tables,columns,table_constraints,key_column_usage,
views}), and connect()'s key-pair-vs-password dispatch was verified
directly against the installed snowflake-connector-python's connection
internals (DEFAULT_CONFIGURATION's accepted kwarg types, and the
authenticator dispatch in SnowflakeConnection.connect()). Unlike
backends/postgres.py and backends/bigquery.py, none of this has been
exercised against a real Snowflake account yet - only against the fake
DB-API harness in tests/server/helpers.py (see test_snowflake_backend.py).
Treat the SQL/kwarg shapes here as a solid first draft, not as already
battle-tested the way the other two backends are.
"""

import sqlparse

from .base import (
    Backend, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
)

# Imported lazily-by-name (module-level, not inside connect()) so tests can
# monkeypatch backends.snowflake.snowflake.connector.connect the same way
# helpers.install_fake_bigquery patches backends.bigquery's bigquery.*
# names - see tests/server/helpers.py.
import snowflake.connector

# The exact literal string snowflake-connector-python's SnowflakeConnection
# checks for to route into key-pair (JWT) auth instead of its default
# (password) authenticator - see SnowflakeConnection.connect() in the
# installed package. Supplying `private_key` without also setting this is
# silently ignored by the connector (it stays on password auth), so this
# is not optional whenever private_key is used.
_KEY_PAIR_AUTHENTICATOR = "SNOWFLAKE_JWT"


class SnowflakeBackend(Backend):
    dialect_name = "Snowflake SQL"

    def connect(self, descriptor):
        descriptor = descriptor or {}
        account = descriptor.get("account") or ""
        user = descriptor.get("user") or ""
        warehouse = descriptor.get("warehouse") or ""
        database = descriptor.get("database") or ""
        schema = descriptor.get("schema") or None
        role = descriptor.get("role") or None
        password = descriptor.get("password") or None
        private_key = descriptor.get("private_key") or None
        private_key_passphrase = descriptor.get("private_key_passphrase") or None

        kwargs = {
            "account": account,
            "user": user,
            "warehouse": warehouse,
            "database": database,
        }
        if schema:
            kwargs["schema"] = schema
        if role:
            kwargs["role"] = role

        if private_key:
            # See _KEY_PAIR_AUTHENTICATOR above - required for the
            # connector to actually use the key rather than silently
            # falling back to password auth (and then failing on a missing
            # password instead).
            kwargs["authenticator"] = _KEY_PAIR_AUTHENTICATOR
            kwargs["private_key"] = private_key
            if private_key_passphrase:
                kwargs["private_key_passphrase"] = private_key_passphrase
        elif password:
            kwargs["password"] = password
        else:
            # Should already be rejected upstream (config_routes.py's
            # equivalent of BigQuery's "requires both a billing project ID
            # and a service-account key" check), but connect() shouldn't
            # silently hand the connector zero credentials and let it fail
            # with a more confusing error either.
            raise ValueError(
                "Snowflake connection requires either 'password' or "
                "'private_key' - neither was provided."
            )

        return snowflake.connector.connect(**kwargs)

    def close(self, connection):
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    def cache_key(self, descriptor):
        """account/database.schema, parsed straight from the descriptor -
        never a credential. Same non-sensitive-identifier role
        PostgresBackend.cache_key's username@dbname and
        BigQueryBackend.cache_key's project.dataset play."""
        descriptor = descriptor or {}
        account = descriptor.get("account") or "unknown"
        database = descriptor.get("database") or "unknown"
        schema = descriptor.get("schema") or "unknown"
        return f"{account}/{database}.{schema}"

    def identity_label(self, connection):
        db_name, username = "Unknown", "Unknown"
        with connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_DATABASE(), CURRENT_USER();")
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
            # (see backends/base.py) *before* the column/constraint query
            # runs - mirrors backends/postgres.py's phased approach.
            # CURRENT_SCHEMA() scopes this to whichever schema the
            # connection actually authenticated into (descriptor's
            # "schema", or the account's default if that was omitted -
            # see connect() above) rather than a hardcoded name the way
            # Postgres's 'public' is, since Snowflake has no single
            # universal default schema name.
            cursor.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = CURRENT_SCHEMA()
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
                LIMIT %s;
            """, (SCHEMA_MAX_TABLE_NAMES_SCANNED,))
            all_table_names = [row[0] for row in cursor.fetchall()]

            if not all_table_names:
                return None

            kept_names, shard_groups = group_date_sharded_tables(all_table_names)
            kept_names, shard_groups, omitted_count = cap_kept_tables(kept_names, shard_groups)
            # No native wildcard-table query mechanism (unlike BigQuery),
            # so a shard family's representative is described under its
            # own real, literal name - mirrors backends/postgres.py.
            shard_by_representative = {
                members[-1]: (prefix, members) for prefix, members in shard_groups.items()
            }

            # 1. Tables and columns - scoped to the bounded kept_names set.
            # Individually-bound placeholders (not a single array parameter
            # - Snowflake's default pyformat paramstyle has no Postgres-
            # style ANY(%s) array-binding equivalent), never string-
            # formatted into the SQL.
            placeholders = ", ".join(["%s"] * len(kept_names))
            cursor.execute(f"""
                SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = CURRENT_SCHEMA()
                  AND table_name IN ({placeholders})
                ORDER BY table_name, ordinal_position;
            """, tuple(kept_names))
            columns_data = cursor.fetchall()

            tables = {}
            for table_name, col_name, data_type, is_nullable in columns_data:
                tables.setdefault(table_name, []).append(
                    f"  {col_name} {data_type} {'NULL' if is_nullable == 'YES' else 'NOT NULL'}"
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

            # 2. Constraints - like BigQuery (and unlike Postgres), Snowflake
            # does not enforce PK/FK/UNIQUE constraints at write time, but
            # they're still useful context for the model. Best-effort: some
            # accounts/roles may not have visibility into
            # KEY_COLUMN_USAGE, so a failure here just skips this section
            # rather than failing the whole schema fetch (mirrors
            # backends/bigquery.py's same try/except).
            try:
                cursor.execute(f"""
                    SELECT tc.table_name, tc.constraint_name, tc.constraint_type, kcu.column_name
                    FROM information_schema.table_constraints tc
                    LEFT JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_schema = CURRENT_SCHEMA()
                      AND tc.table_name IN ({placeholders})
                    ORDER BY tc.table_name, tc.constraint_name;
                """, tuple(kept_names))
                constraint_rows = cursor.fetchall()
                if constraint_rows:
                    lines = [
                        f"  [{t}] {n} ({ty}): {c}" for (t, n, ty, c) in constraint_rows
                    ]
                    schema_parts.append("Constraints:\n" + "\n".join(lines))
            except Exception:
                pass

            # 3. Views - deliberately NOT scoped to kept_names, same
            # reasoning as backends/postgres.py/backends/bigquery.py: that
            # set is built exclusively from BASE TABLE names, so no view
            # name could ever appear in it.
            try:
                cursor.execute("""
                    SELECT table_name, view_definition
                    FROM information_schema.views
                    WHERE table_schema = CURRENT_SCHEMA();
                """)
                view_rows = cursor.fetchall()
                if view_rows:
                    schema_parts.append(
                        "Views:\n" + "\n".join(
                            f"  View {t}: {(d or '').strip()}" for (t, d) in view_rows
                        )
                    )
            except Exception:
                pass

            # Deliberately no Indexes/Triggers/Grants sections: Snowflake
            # has no user-managed indexes (automatic micro-partition
            # pruning/clustering instead - nothing to introspect the way
            # Postgres's pg_indexes describes) and no trigger support at
            # all. A grants section (like Postgres's role_table_grants) is
            # left for follow-up - Snowflake's INFORMATION_SCHEMA grants
            # views weren't verified against a real account as part of
            # this first pass (see module docstring).

        if not schema_parts:
            return None
        return cap_schema_text("\n\n".join(schema_parts))

    def execute(self, connection, sql_text):
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
                    count = row_count if row_count is not None and row_count >= 0 else 0

                results.append({
                    'statement': stmt_clean,
                    'columns': columns,
                    'rows': rows,
                    'rowCount': count,
                })

        return results
