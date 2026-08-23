"""
backends/databricks.py

DatabricksBackend: talks to a Databricks SQL Warehouse (or all-purpose
cluster - the driver doesn't distinguish, both are addressed by an
http_path) via databricks-sql-connector, the official pure-Python driver
(no compiled extension - like snowflake-connector-python/PyMySQL, this
needed no Dockerfile changes to add).

Mirrors backends/snowflake.py's shape more than backends/postgres.py's:
like Snowflake, Databricks has no single connection-string form and no
BigQuery-style ambient identity to fall back to - every connection, preset
or custom, needs its own explicit credential. Unlike Snowflake, this first
pass supports exactly one auth method: a Personal Access Token
(descriptor's "access_token"). Databricks also supports OAuth (an
interactive user flow, or client-credentials/service-principal M2M auth),
but that's meaningfully more machinery (token exchange, refresh handling)
than a first cut needs - PAT-only mirrors how Snowflake's own first pass
only supported password/key-pair, not SSO.

A Databricks descriptor looks like:
    {"type": "databricks", "server_hostname": "...", "http_path": "...",
     "access_token": "...", "catalog": "...", "schema": "..."}
"server_hostname"/"http_path"/"access_token" are required - server_hostname
is the workspace URL (e.g. "dbc-a1b2c3d4-e5f6.cloud.databricks.com", no
scheme), http_path identifies which SQL Warehouse/cluster to route queries
to (e.g. "/sql/1.0/warehouses/0123456789abcdef" for a warehouse). "catalog"
and "schema" are optional: omitted, the connection falls back to whatever
the workspace/warehouse's own default namespace is (commonly
"hive_metastore.default" on a non-Unity-Catalog workspace, or the
workspace's configured default catalog under Unity Catalog).

Databricks is a three-level namespace (catalog.schema.table), unlike
Postgres's two-level (schema.table under a single connected database) or
MySQL's single-level (a "schema" IS a database) - see
translate_routes.py's _DIALECT_PROMPT_INTROS entry for this dialect, which
tells Gemini to always qualify with catalog.schema.table rather than the
two-part form other dialects use. Identifier quoting uses backticks, same
as MySQL/BigQuery.

Which of "access_token" must never round-trip back to the frontend once
saved is state_store.py's _CREDENTIAL_CONFIG_FIELDS' responsibility,
mirrored from how it already handles bigquery.py's "credentials_json" and
snowflake.py's "password"/"private_key" - see that module's docstring.

The connector's declared DB-API paramstyle is "named" (:name placeholders),
NOT the "%s"/pyformat style Postgres/MySQL/Snowflake's connectors use in
this codebase - get_schema() below builds its dynamic IN (...) clauses
accordingly (see _named_in_params). Also unlike those three, the
connector's Connection.autocommit is a read-only property, not something
execute() can set - Databricks SQL warehouses have no traditional
transaction/autocommit toggle to configure in the first place, so
execute() below simply has nothing to do there.

NOTE for reviewers: like backends/snowflake.py, this has been exercised
against the fake DB-API harness in tests/server/helpers.py, not a real
Databricks workspace yet - treat the SQL/kwarg shapes here as a solid
first draft, not as already battle-tested the way backends/postgres.py is.
"""

import databricks.sql as databricks_sql
import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
)


def _named_in_params(prefix, values):
    """(fragment, params) for a dynamic IN (...) clause under the
    connector's "named" paramstyle (:name, not %s/?) - e.g. for
    values=["a", "b"] and prefix="t", returns (":t0, :t1", {"t0": "a",
    "t1": "b"}). Used wherever get_schema() below needs to scope a query
    to the bounded kept_names set (see backends/base.py)."""
    names = [f"{prefix}{i}" for i in range(len(values))]
    fragment = ", ".join(f":{n}" for n in names)
    return fragment, dict(zip(names, values))


class DatabricksBackend(Backend):
    dialect_name = "Databricks SQL"

    def connect(self, descriptor):
        descriptor = descriptor or {}
        server_hostname = descriptor.get("server_hostname") or ""
        http_path = descriptor.get("http_path") or ""
        access_token = descriptor.get("access_token") or None
        catalog = descriptor.get("catalog") or None
        schema = descriptor.get("schema") or None

        if not access_token:
            # Should already be rejected upstream (config_routes.py's
            # equivalent of Snowflake's "requires either 'password' or
            # 'private_key'" check), but connect() shouldn't silently hand
            # the driver zero credentials and let it fail with a more
            # confusing error either.
            raise ValueError(
                "Databricks connection requires an access_token - none was provided."
            )

        # No connect-only timeout kwarg here, unlike every other network-
        # dialing backend (see backends/base.py's DB_CONNECT_TIMEOUT_SECONDS
        # docstring) - deliberately, not an oversight. This connector's only
        # relevant knob (undocumented "_socket_timeout") bounds socket send/
        # recv/connect for the connection's *entire* lifetime, not just the
        # initial handshake, so setting it here would also cap how long any
        # query run over this same connection is allowed to take. Capping a
        # bad preset's *connect* attempt isn't worth silently truncating a
        # legitimate long-running query on a *working* Databricks connection
        # - a wrong/unreachable Databricks preset still fails eventually via
        # the connector's own (much longer) internal timeouts, and every
        # caller of connect() already wraps it in try/except and degrades
        # gracefully (see execute_routes.py's ping()/config_routes.py's
        # handle_config()), so the failure mode is "that one preset is slow
        # to report broken," not "the whole app hangs."
        kwargs = {"server_hostname": server_hostname, "http_path": http_path, "access_token": access_token}
        if catalog:
            kwargs["catalog"] = catalog
        if schema:
            kwargs["schema"] = schema
        return databricks_sql.connect(**kwargs)

    def close(self, connection):
        # hasattr-guarded like backends/bigquery.py's/backends/snowflake.py's
        # close() (not just `if connection:` the way backends/postgres.py's
        # does) - config_routes.py's /api/config handler calls this
        # unconditionally in a finally block after a best-effort
        # identity_label() probe, including in tests that patch connect()
        # with a lightweight stand-in object that has no close() of its own
        # (see helpers.install_fake_databricks_connect).
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    def cache_key(self, descriptor):
        """server_hostname/catalog.schema, parsed straight from the
        descriptor - never a credential. Same non-sensitive-identifier role
        SnowflakeBackend.cache_key's account/database.schema plays."""
        descriptor = descriptor or {}
        host = descriptor.get("server_hostname") or "unknown"
        catalog = descriptor.get("catalog") or "unknown"
        schema = descriptor.get("schema") or "unknown"
        return f"{host}/{catalog}.{schema}"

    def identity_label(self, connection):
        db_name, username = "Unknown", "Unknown"
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_catalog(), current_user();")
            row = cursor.fetchone()
            if row:
                db_name, username = row[0], row[1]
        return db_name, username

    def get_schema(self, connection):
        schema_parts = []

        with connection.cursor() as cursor:
            # Phase 1: cheap - just the distinct base-table names, bounded
            # so a schema with an extreme number of tables can't make even
            # this scan unbounded (SCHEMA_MAX_TABLE_NAMES_SCANNED). Grouped
            # into date-shard families and capped to SCHEMA_MAX_TABLES
            # entries (see backends/base.py) before any column/constraint/
            # view query runs, same staging as backends/postgres.py's/
            # backends/snowflake.py's get_schema(). current_catalog()/
            # current_schema() scope this to whichever namespace the
            # connection actually authenticated into (descriptor's
            # "catalog"/"schema", or the workspace's default if omitted -
            # see connect() above) rather than a hardcoded name.
            #
            # table_type is NOT the ANSI-standard 'BASE TABLE' value every
            # other dialect here uses (Postgres/MySQL/Snowflake/BigQuery) -
            # Databricks' information_schema.tables instead reports ordinary
            # tables as 'MANAGED' or 'EXTERNAL' (or their shallow-clone
            # variants), reserving 'VIEW'/'STREAMING_TABLE'/
            # 'MATERIALIZED_VIEW'/'FOREIGN' for everything else - see
            # https://docs.databricks.com/aws/en/sql/language-manual/information-schema/tables.
            # Filtering on 'BASE TABLE' here silently matched zero rows
            # against a real workspace (this was caught after shipping,
            # against a real connection - the fake DB-API harness in
            # tests/server/helpers.py has no opinion on table_type values,
            # so nothing here would have failed a test either way).
            cursor.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_catalog = current_catalog()
                  AND table_schema = current_schema()
                  AND table_type IN ('MANAGED', 'EXTERNAL', 'MANAGED_SHALLOW_CLONE', 'EXTERNAL_SHALLOW_CLONE')
                ORDER BY table_name
                LIMIT :scan_limit;
            """, {"scan_limit": SCHEMA_MAX_TABLE_NAMES_SCANNED})
            all_table_names = [row[0] for row in cursor.fetchall()]

            if not all_table_names:
                return None

            kept_names, shard_groups = group_date_sharded_tables(all_table_names)
            kept_names, shard_groups, omitted_count = cap_kept_tables(kept_names, shard_groups)
            # No native wildcard-table query mechanism (unlike BigQuery), so
            # a shard family's representative is described under its own
            # real, literal name - mirrors backends/postgres.py/
            # backends/snowflake.py.
            shard_by_representative = {
                members[-1]: (prefix, members) for prefix, members in shard_groups.items()
            }

            # 1. Tables and columns - scoped to the bounded kept_names set.
            in_fragment, in_params = _named_in_params("t", kept_names)
            cursor.execute(f"""
                SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_catalog = current_catalog()
                  AND table_schema = current_schema()
                  AND table_name IN ({in_fragment})
                ORDER BY table_name, ordinal_position;
            """, in_params)
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

            # 2. Constraints - like BigQuery/Snowflake (and unlike
            # Postgres/MySQL), Databricks' Unity Catalog constraints are
            # informational only, not enforced at write time, but still
            # useful context for the model. Best-effort: a non-Unity-Catalog
            # workspace (hive_metastore only) may not expose these views at
            # all, so a failure here just skips this section rather than
            # failing the whole schema fetch (mirrors backends/bigquery.py's/
            # backends/snowflake.py's same try/except).
            try:
                cursor.execute(f"""
                    SELECT tc.table_name, tc.constraint_name, tc.constraint_type, kcu.column_name
                    FROM information_schema.table_constraints tc
                    LEFT JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_catalog = kcu.table_catalog
                    WHERE tc.table_catalog = current_catalog()
                      AND tc.table_schema = current_schema()
                      AND tc.table_name IN ({in_fragment})
                    ORDER BY tc.table_name, tc.constraint_name;
                """, in_params)
                constraint_rows = cursor.fetchall()
                if constraint_rows:
                    lines = [f"  [{t}] {n} ({ty}): {c}" for (t, n, ty, c) in constraint_rows]
                    schema_parts.append("Constraints:\n" + "\n".join(lines))
            except Exception:
                pass

            # 3. Views - deliberately NOT scoped to kept_names, same
            # reasoning as backends/postgres.py/backends/snowflake.py: that
            # set is built exclusively from ordinary-table table_type values
            # (see the query above), so no view name could ever appear in
            # it.
            try:
                cursor.execute("""
                    SELECT table_name, view_definition
                    FROM information_schema.views
                    WHERE table_catalog = current_catalog()
                      AND table_schema = current_schema();
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

            # Deliberately no Indexes/Triggers/Grants sections: Databricks
            # SQL has no user-managed indexes to introspect (automatic
            # file/partition pruning instead) and no trigger support at
            # all. A grants section is left for follow-up, same status
            # backends/snowflake.py's grants support has - not verified
            # against a real workspace as part of this first pass.

        if not schema_parts:
            return None
        return cap_schema_text("\n\n".join(schema_parts))

    def execute(self, connection, sql_text):
        # No autocommit call here (unlike backends/postgres.py's/
        # backends/mysql.py's execute()) - see module docstring: Databricks
        # SQL warehouses have no traditional transaction/autocommit toggle
        # for a connection to set in the first place.
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
