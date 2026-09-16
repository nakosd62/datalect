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

--- Two-phase schema introspection (get_schema_shallow / get_schema) -------
Like every other SQL backend here, schema introspection is split into a
cheap, catalog-only Phase 1 (_build_shallow_schema_parts, shared by both
entry points) and a richer, live-query Phase 2 (appended only by
get_schema(), the "deep" fetch) - see backends/base.py's
Backend.get_schema()/get_schema_shallow() docstrings for why this split
exists at all, and backends/postgres.py's own _build_shallow_schema_parts
for the worked pattern this mirrors.

Every Unity Catalog information_schema column name referenced below (
IS_IDENTITY, comment, partition_index, routines/parameters,
table_privileges) is taken from Databricks' documented Unity Catalog
information_schema shape (which is itself closely ANSI/Postgres-shaped),
not verified against a live workspace from this sandbox - see the module
docstring above ("exercised against the fake DB-API harness ... not a real
Databricks workspace yet"), same caveat, now extended to these new
sections too.
"""

import databricks.sql as databricks_sql
import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text, fetch_capped_rows,
    find_naming_convention_relationships,
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


def _quote_ident(name):
    """Backtick-quotes a Databricks/Spark SQL identifier for interpolation
    into a plain SQL string (escaping an embedded '`' by doubling it, the
    way Spark SQL itself expects) - used by get_schema()'s Phase 2 per-table
    live-query section below, mirroring backends/postgres.py's own
    _quote_ident (double-quote there, backtick here - see module docstring's
    "Identifier quoting uses backticks" note). `name` always comes from
    information_schema data this same connection already queried
    (kept_names / column names Phase 1 just fetched) - never from raw user
    input - so this only needs to be correct, not defend against
    adversarial identifiers."""
    return "`" + str(name).replace("`", "``") + "`"


# Phase 2 (deep-only) sampling: which information_schema.columns.data_type
# strings are worth a MIN()/MAX() range query (numeric/date-ish) vs a
# frequent-value GROUP BY (bounded/categorical-ish) - see get_schema()'s
# "Column value samples" section below. Matched case-insensitively, and
# DECIMAL(p,s)/VARCHAR(n)/CHAR(n) are matched by prefix since Databricks'
# data_type text includes the precision/scale/length rather than being a
# bare type name for those three. Deliberately conservative - a data_type
# this doesn't recognize (e.g. "array<string>", "map<string,string>",
# "binary", "struct<...>") is simply skipped for sampling rather than
# guessed at, since an ill-fitting MIN()/MAX() or GROUP BY on the wrong
# shape of column is more likely to error or produce noise than a useful
# hint.
_NUMERIC_OR_DATE_TYPES = frozenset({
    "tinyint", "smallint", "int", "integer", "bigint", "float", "double", "real",
    "date", "timestamp", "timestamp_ntz", "timestamp_ltz",
})
_CATEGORICAL_TYPES = frozenset({"string", "boolean"})


def _is_numeric_or_date_type(data_type):
    t = (data_type or "").strip().lower()
    if t.startswith("decimal") or t.startswith("numeric"):
        return True
    return t in _NUMERIC_OR_DATE_TYPES


def _is_categorical_type(data_type):
    t = (data_type or "").strip().lower()
    if t.startswith("varchar") or t.startswith("char"):
        return True
    return t in _CATEGORICAL_TYPES


# Bounds on Phase 2's per-table sampling cost, all deliberately small - see
# get_schema()'s "Column value samples" section for how each is used.
# Mirrors backends/postgres.py's own constants of the same name/purpose.
MAX_COLUMNS_FOR_SAMPLING = 25
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
# How many categorical columns (in column order) get an APPROX_COUNT_DISTINCT
# cardinality-gate check per table before the (smaller)
# MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE cap picks which of those actually
# get a "frequent values" GROUP BY - Databricks has no free planner
# statistic like Postgres's pg_stats.n_distinct to gate on for free, so this
# bounds the (still live, but cheaper-than-COUNT-DISTINCT)
# APPROX_COUNT_DISTINCT probe itself to a small, fixed-size candidate pool
# rather than running it against every categorical column on a wide table.
CARDINALITY_CANDIDATE_COLUMNS_PER_TABLE = 8
FREQUENT_VALUES_LIMIT = 15
# Cardinality gate thresholds for _is_near_unique_approx below - a column
# whose approx-distinct count is either an outright large number or a large
# fraction of the table's own (already-fetched) live row count is treated as
# "too close to unique to be worth a frequent-values sample", same intent as
# backends/postgres.py's _is_near_unique_n_distinct but computed from a live
# APPROX_COUNT_DISTINCT() probe instead of a free planner statistic.
NEAR_UNIQUE_DISTINCT_RATIO = 0.5
NEAR_UNIQUE_ABSOLUTE_DISTINCT = 1000


def _is_near_unique_approx(distinct_count, live_count):
    if distinct_count is None:
        return False
    if not live_count or live_count <= 0:
        return distinct_count > NEAR_UNIQUE_ABSOLUTE_DISTINCT
    return (
        distinct_count > NEAR_UNIQUE_ABSOLUTE_DISTINCT
        or (distinct_count / live_count) > NEAR_UNIQUE_DISTINCT_RATIO
    )


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

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - see the module-level docstring's note on
        the two-phase split and backends/base.py's Backend.get_schema()/
        get_schema_shallow() docstrings for why this split exists at all.

        Returns None if the connection's current_catalog()/current_schema()
        has no in-scope table at all (mirrors get_schema()'s old
        "return None" for that case). Otherwise returns
        (schema_parts, table_columns, phase2_ctx):
          - schema_parts: the ordered list of text sections, not yet joined/
            capped.
          - table_columns: {table_name: [column_name, ...]}, scoped to the
            same bounded kept_names set schema_parts describes - handed to
            the shared find_naming_convention_relationships() helper by
            get_schema()'s Phase 2 pass.
          - phase2_ctx: kept_names/column_types (for deciding what to
            sample) plus the raw views/routines rows, so get_schema() (deep)
            can render their full body text without a second query.
        """
        schema_parts = []
        table_columns = {}
        column_types = {}

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
            #
            # 'FOREIGN' (an external/federated table registered via Lakehouse
            # Federation) used to be excluded from this IN (...) list
            # entirely - the model never saw such a table exist at all. That
            # silently hid real, queryable tables rather than describing them
            # - flipped here to INCLUDE 'FOREIGN' in the scan (it now counts
            # toward kept_names/SCHEMA_MAX_TABLES like any other table) and
            # instead flag it on its own "Table:" heading below (see the
            # foreign_tables set and the heading-building loop), so the model
            # knows the table exists and that it's external rather than
            # native to this workspace, instead of never seeing it at all.
            cursor.execute("""
                SELECT table_name, table_type, comment
                FROM information_schema.tables
                WHERE table_catalog = current_catalog()
                  AND table_schema = current_schema()
                  AND table_type IN ('MANAGED', 'EXTERNAL', 'MANAGED_SHALLOW_CLONE', 'EXTERNAL_SHALLOW_CLONE', 'FOREIGN')
                ORDER BY table_name
                LIMIT :scan_limit;
            """, {"scan_limit": SCHEMA_MAX_TABLE_NAMES_SCANNED})
            table_rows_raw = cursor.fetchall()
            all_table_names = [row[0] for row in table_rows_raw]
            foreign_tables = {row[0] for row in table_rows_raw if row[1] == 'FOREIGN'}
            table_comments = {row[0]: row[2] for row in table_rows_raw if row[2]}

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
            # is_identity (new) marks a Delta identity column (Unity
            # Catalog's IS_IDENTITY, "YES"/"NO") - annotated inline on the
            # column's own line below, same spot backends/postgres.py's
            # IDENTITY marker goes. comment (new) is this column's Unity
            # Catalog comment, rendered in the "Comments:" section below
            # rather than inline (mirrors backends/postgres.py's own
            # Comments section). partition_index (new) is non-NULL for a
            # Delta partition column, its value giving that column's
            # position within the partitioning key - used to build the
            # "Partition columns:" section below, Databricks' equivalent of
            # a distribution/sort/clustering key (Delta doesn't have
            # Redshift-style distribution/sort keys - partitioning IS the
            # relevant mechanism here, per automatic file/partition pruning;
            # see the old "Deliberately no Indexes" comment this replaces).
            in_fragment, in_params = _named_in_params("t", kept_names)
            cursor.execute(f"""
                SELECT table_name, column_name, data_type, is_nullable, is_identity, comment, partition_index
                FROM information_schema.columns
                WHERE table_catalog = current_catalog()
                  AND table_schema = current_schema()
                  AND table_name IN ({in_fragment})
                ORDER BY table_name, ordinal_position;
            """, in_params)
            columns_data = cursor.fetchall()

            tables = {}
            column_comment_lines = []
            partition_cols = {}
            for row in columns_data:
                (table_name, col_name, data_type, is_nullable, is_identity, comment, partition_index) = row
                tables.setdefault(table_name, [])
                table_columns.setdefault(table_name, []).append(col_name)
                column_types.setdefault(table_name, {})[col_name] = data_type
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                identity_str = " IDENTITY" if is_identity in ("YES", True) else ""
                tables[table_name].append(
                    f"  {col_name} {data_type} {null_str}{identity_str}"
                )
                if comment:
                    column_comment_lines.append(f"  [column] {table_name}.{col_name}: {comment}")
                if partition_index is not None:
                    partition_cols.setdefault(table_name, []).append((partition_index, col_name))

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
                elif table_name in foreign_tables:
                    # The FOREIGN-table flip (see the table-scan query's own
                    # comment above): this table used to never appear in
                    # schema text at all - now it's described like any other
                    # table, just flagged as external/federated so the model
                    # knows not to treat it as workspace-native storage.
                    heading = (
                        f"Table: {table_name} "
                        f"[EXTERNAL/FOREIGN - federated table via Lakehouse "
                        f"Federation, not stored in this workspace]"
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
            #
            # Shallow rendering is name-only (no view_definition body) - the
            # raw rows (including each view's body) are still fetched here
            # (one query, reused by both phases) and threaded through via
            # phase2_ctx below so get_schema() (deep) can render the full
            # body without a second query - see get_schema()'s "View
            # definitions" section.
            views = []
            try:
                cursor.execute("""
                    SELECT table_name, view_definition
                    FROM information_schema.views
                    WHERE table_catalog = current_catalog()
                      AND table_schema = current_schema();
                """)
                views = cursor.fetchall()
                if views:
                    schema_parts.append(
                        "Views:\n" + "\n".join(f"  View {t}" for (t, _d) in views)
                    )
            except Exception:
                pass

            # 4. Comments (new) - table and column comments, from the two
            # queries already run above (no extra query needed): Unity
            # Catalog's information_schema.tables.comment /
            # information_schema.columns.comment. Only rendered for kept
            # tables (table_comments was built from every scanned table
            # name, before capping).
            comment_lines = []
            for t in kept_names:
                c = table_comments.get(t)
                if c:
                    comment_lines.append(f"  [table] {t}: {c}")
            comment_lines.extend(column_comment_lines)
            if comment_lines:
                schema_parts.append("Comments:\n" + "\n".join(comment_lines))

            # 5. Partition columns (new) - Databricks' distribution/sort/
            # clustering-key equivalent (Delta doesn't have Redshift-style
            # distribution/sort keys - partitioning is what actually governs
            # file/partition pruning here). Built from partition_index,
            # already fetched in the columns query above - no extra query.
            if partition_cols:
                partition_lines = []
                for t in kept_names:
                    cols = partition_cols.get(t)
                    if not cols:
                        continue
                    ordered = [c for _idx, c in sorted(cols, key=lambda pair: pair[0])]
                    partition_lines.append(f"  {t}: {', '.join(ordered)}")
                if partition_lines:
                    schema_parts.append("Partition columns:\n" + "\n".join(partition_lines))

            # 6. Routines (new) - existence + signature only, no body (see
            # get_schema()'s "Routine definitions" section for the full-body
            # deep-only counterpart, reusing routine_definition fetched here
            # rather than re-querying it). Not scoped to kept_names (like
            # Views above) - routines aren't tables and
            # current_catalog()/current_schema() alone already bound this to
            # the connection's own namespace. Best-effort: Unity Catalog's
            # information_schema.routines/parameters views may not be
            # available on every workspace/permission configuration.
            routines = []
            try:
                cursor.execute("""
                    SELECT r.specific_name, r.routine_name, r.data_type, r.routine_definition
                    FROM information_schema.routines r
                    WHERE r.routine_catalog = current_catalog()
                      AND r.routine_schema = current_schema()
                    ORDER BY r.routine_name;
                """)
                routine_rows = cursor.fetchall()
                cursor.execute("""
                    SELECT specific_name, parameter_name, data_type, ordinal_position
                    FROM information_schema.parameters
                    WHERE specific_catalog = current_catalog()
                      AND specific_schema = current_schema()
                    ORDER BY specific_name, ordinal_position;
                """)
                param_rows = cursor.fetchall()

                params_by_specific = {}
                for specific_name, param_name, param_type, _pos in param_rows:
                    params_by_specific.setdefault(specific_name, []).append(f"{param_name} {param_type}")

                for specific_name, routine_name, return_type, routine_definition in routine_rows:
                    signature = ", ".join(params_by_specific.get(specific_name, []))
                    routines.append((routine_name, signature, return_type, routine_definition))

                if routines:
                    schema_parts.append(
                        "Routines:\n" + "\n".join(f"  {n}({sig}) -> {rt}" for n, sig, rt, _def in routines)
                    )
            except Exception:
                routines = []

            # 7. Grants (new) - Unity Catalog's information_schema.
            # table_privileges (an ANSI-standard-named view, unlike
            # Postgres's own non-standard role_table_grants) - scoped to
            # kept_names, one line per (table, grantee, privilege), same
            # "at most one line per table"-ish guidance as the plan this
            # implements. Best-effort: Unity Catalog's permission model
            # varies by metastore/workspace configuration, and a role
            # without USE CATALOG/schema-level visibility into grants may
            # not be able to query this at all.
            try:
                cursor.execute(f"""
                    SELECT grantee, table_name, privilege_type
                    FROM information_schema.table_privileges
                    WHERE table_catalog = current_catalog()
                      AND table_schema = current_schema()
                      AND table_name IN ({in_fragment})
                    ORDER BY table_name, grantee;
                """, in_params)
                grants = cursor.fetchall()
                if grants:
                    grant_lines = [f"  Grant {g[2]} on {g[1]} to {g[0]}" for g in grants]
                    schema_parts.append("Grants:\n" + "\n".join(grant_lines))
            except Exception:
                pass

            # Deliberately no Indexes/Triggers sections: Databricks SQL has
            # no user-managed indexes to introspect (automatic file/
            # partition pruning instead - see "Partition columns" above) and
            # no trigger support at all.
            #
            # Deliberately no session-timezone/collation section either
            # (unlike backends/postgres.py's "Session:" line): Spark SQL's
            # session timezone is a Spark/cluster configuration value
            # (spark.sql.session.timeZone), not a reliably queryable session
            # variable the way Postgres's current_setting('TimeZone') is -
            # fabricating a query for this without being able to verify it
            # against a real workspace risked shipping something silently
            # wrong rather than silently absent, so this is skipped rather
            # than guessed at.
            #
            # Deliberately no row-count-estimate section either (unlike
            # backends/postgres.py's pg_class.reltuples): there's no
            # confirmed cheap, catalog-only source for this on Databricks -
            # DESCRIBE DETAIL reports file/size statistics for a Delta
            # table, not a row count, and would need one query per table
            # (not a single information_schema scan) besides. See
            # get_schema()'s "Live row counts" section below for the
            # authoritative, deep-only counterpart (a live COUNT(*) per
            # kept table) instead.
            #
            # Deliberately no RLS/column-masking existence flag either:
            # Unity Catalog row filters/column masks have no equivalent to
            # Postgres's cheap pg_policies/pg_class.relrowsecurity catalog
            # lookup that this sandbox could confirm is real and cheap to
            # query - see the FOREIGN/external flag above for the one flag
            # in this attribute group that *is* cheaply determinable from a
            # query this file already runs.

        phase2_ctx = {
            "kept_names": kept_names,
            "column_types": column_types,
            "views": views,
            "routines": routines,
        }
        return schema_parts, table_columns, phase2_ctx

    def get_schema_shallow(self, connection):
        built = self._build_shallow_schema_parts(connection)
        if built is None:
            return None
        schema_parts, _table_columns, _phase2_ctx = built
        if not schema_parts:
            return None
        return cap_schema_text("\n\n".join(schema_parts))

    def get_schema(self, connection):
        built = self._build_shallow_schema_parts(connection)
        if built is None:
            return None
        schema_parts, table_columns, phase2_ctx = built
        schema_parts = list(schema_parts)

        kept_names = phase2_ctx["kept_names"]
        column_types = phase2_ctx["column_types"]
        views = phase2_ctx["views"]
        routines = phase2_ctx["routines"]

        # Phase 2 (deep-only): full view/routine bodies, reusing the raw
        # rows _build_shallow_schema_parts already fetched - no re-query.
        if views:
            view_lines = [f"  View {v[0]}: {(v[1] or '').strip()}" for v in views]
            # view_definition can legitimately come back NULL (not just an
            # empty string) when the connected role lacks the privilege to
            # see a given view's definition - `(v[1] or '').strip()` guards
            # against that, mirroring every other backend's own views-
            # section guard.
            schema_parts.append("View definitions:\n" + "\n".join(view_lines))

        routine_body_lines = [
            f"  {r[0]}: {(r[3] or '').strip()}" for r in routines if (r[3] or "").strip()
        ]
        if routine_body_lines:
            schema_parts.append("Routine definitions:\n" + "\n".join(routine_body_lines))

        with connection.cursor() as cursor:
            live_count_lines = []
            sample_blocks = []
            live_counts = {}

            for table_name in kept_names:
                col_types = column_types.get(table_name) or {}
                if not col_types:
                    # A kept_names entry cap_kept_tables dropped columns for
                    # (shouldn't normally happen - kept_names and
                    # column_types are built from the same query - but
                    # guards against an empty/omitted table cleanly).
                    continue

                # Fresh/live row count - authoritative. There's no Phase 1
                # estimate to compare against on this dialect (see
                # _build_shallow_schema_parts' own comment on why that
                # section was skipped as too uncertain) - labeled
                # "authoritative" regardless, same wording every other
                # backend uses for its own live count.
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {_quote_ident(table_name)};")
                    row = cursor.fetchone()
                    if row is not None:
                        live_counts[table_name] = row[0]
                        live_count_lines.append(f"  {table_name}: {row[0]} rows (live, authoritative)")
                except Exception:
                    pass

                if len(col_types) > MAX_COLUMNS_FOR_SAMPLING:
                    # Table too wide to sample column-by-column without an
                    # explosion of tiny queries - still gets its live count
                    # above, just no per-column sampling below.
                    continue

                numeric_cols = [
                    c for c, t in col_types.items() if _is_numeric_or_date_type(t)
                ][:MAX_NUMERIC_COLUMNS_FOR_MINMAX]
                categorical_cols = [c for c, t in col_types.items() if _is_categorical_type(t)]

                table_sample_lines = []

                # Min/max, all eligible numeric/date columns in one combined
                # query per table (bounded by MAX_NUMERIC_COLUMNS_FOR_MINMAX)
                # rather than one query per column.
                if numeric_cols:
                    try:
                        select_parts = ", ".join(
                            f"MIN({_quote_ident(c)}) AS min_{i}, MAX({_quote_ident(c)}) AS max_{i}"
                            for i, c in enumerate(numeric_cols)
                        )
                        cursor.execute(f"SELECT {select_parts} FROM {_quote_ident(table_name)};")
                        row = cursor.fetchone()
                        if row is not None:
                            for i, c in enumerate(numeric_cols):
                                min_v, max_v = row[2 * i], row[2 * i + 1]
                                table_sample_lines.append(f"    {c}: range [{min_v} .. {max_v}]")
                    except Exception:
                        pass

                # Cardinality gate - APPROX_COUNT_DISTINCT(col) (Spark SQL's
                # cheaper alternative to an exact COUNT(DISTINCT col) scan)
                # over a small, bounded candidate pool
                # (CARDINALITY_CANDIDATE_COLUMNS_PER_TABLE), one combined
                # query per table. Only the (still smaller)
                # MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE columns that pass
                # the gate go on to get their own "frequent values" GROUP BY
                # query below.
                eligible_categorical = []
                candidate_categorical = categorical_cols[:CARDINALITY_CANDIDATE_COLUMNS_PER_TABLE]
                if candidate_categorical:
                    try:
                        select_parts = ", ".join(
                            f"APPROX_COUNT_DISTINCT({_quote_ident(c)}) AS dc_{i}"
                            for i, c in enumerate(candidate_categorical)
                        )
                        cursor.execute(f"SELECT {select_parts} FROM {_quote_ident(table_name)};")
                        row = cursor.fetchone()
                        if row is not None:
                            live_count = live_counts.get(table_name)
                            for i, c in enumerate(candidate_categorical):
                                if _is_near_unique_approx(row[i], live_count):
                                    continue
                                eligible_categorical.append(c)
                                if len(eligible_categorical) >= MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE:
                                    break
                    except Exception:
                        pass

                for c in eligible_categorical:
                    try:
                        cursor.execute(
                            f"SELECT {_quote_ident(c)}, COUNT(*) FROM {_quote_ident(table_name)} "
                            f"GROUP BY {_quote_ident(c)} ORDER BY COUNT(*) DESC LIMIT {FREQUENT_VALUES_LIMIT};"
                        )
                        freq_rows = cursor.fetchall()
                        if freq_rows:
                            freq_text = ", ".join(f"{val} ({cnt})" for val, cnt in freq_rows)
                            table_sample_lines.append(f"    {c}: frequent values = {freq_text}")
                    except Exception:
                        pass

                if table_sample_lines:
                    sample_blocks.append(f"  Table: {table_name}\n" + "\n".join(table_sample_lines))

            if live_count_lines:
                schema_parts.append("Live row counts:\n" + "\n".join(live_count_lines))
            if sample_blocks:
                schema_parts.append("Column value samples:\n" + "\n\n".join(sample_blocks))

        # Naming-convention relationship pass (shared helper, no new SQL) -
        # pure heuristic over table_columns, which _build_shallow_schema_parts
        # already assembled from Phase 1's own column query.
        relationships = find_naming_convention_relationships(table_columns)
        if relationships:
            schema_parts.append(
                "Likely relationships (naming convention, unconfirmed):\n"
                + "\n".join(f"  {r}" for r in relationships)
            )

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
                    truncated = False

                    if cursor.description:
                        # fetch_capped_rows (backends/base.py) - never a
                        # bare cursor.fetchall() here; see
                        # EXECUTE_RESULTS_MAX_ROWS's own docstring for why.
                        columns, rows, truncated = fetch_capped_rows(cursor)
                        count = len(rows)
                    else:
                        count = row_count if row_count >= 0 else 0

                    result_entry = {
                        'statement': stmt_clean,
                        'columns': columns,
                        'rows': rows,
                        'rowCount': count,
                    }
                    if truncated:
                        result_entry['truncated'] = True
                    results.append(result_entry)
                except Exception as e:
                    # Don't let a mid-script failure silently drop every
                    # result already collected in `results` - see
                    # SqlExecutionError's docstring in backends/base.py.
                    raise SqlExecutionError(str(e), results, stmt_clean, len(results), len(statements)) from e

        return results
