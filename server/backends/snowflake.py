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

Schema introspection is split into two phases (see backends/base.py's
Backend.get_schema()/get_schema_shallow() docstrings for the shared
rationale): _build_shallow_schema_parts() below runs every catalog-only
("Phase 1") query exactly once, shared by both get_schema_shallow() (Phase
1 only - used for connections db.py's all-dbs triage may not even route to)
and get_schema() (Phase 1 + Phase 2's live queries - full/authoritative row
counts, column value samples, naming-convention relationships).

NOTE for reviewers: this module's get_schema()/execute()/identity_label()
queries are written from Snowflake's documented INFORMATION_SCHEMA/session-
function behavior (CURRENT_SCHEMA(), CURRENT_DATABASE(), CURRENT_USER(),
information_schema.{tables,columns,table_constraints,key_column_usage,
views,procedures,functions}), and connect()'s key-pair-vs-password dispatch
was verified directly against the installed snowflake-connector-python's
connection internals (DEFAULT_CONFIGURATION's accepted kwarg types, and the
authenticator dispatch in SnowflakeConnection.connect()). Unlike
backends/postgres.py and backends/bigquery.py, none of this has been
exercised against a real Snowflake account yet - only against the fake
DB-API harness in tests/server/helpers.py (see test_snowflake_backend.py).
Treat the SQL/kwarg shapes here as a solid first draft, not as already
battle-tested the way the other two backends are. This is doubly true for
the newer, best-effort-only sections below (Grants, Row-level security /
masking, Routines) - each is wrapped in try/except and degrades silently
rather than failing the whole schema fetch, precisely because the exact
catalog/SHOW-command shape for those is the least certain part of this
module; see each section's own comment for the specific catalog objects it
assumes exist and are queryable by the connected role.
"""

import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS, resolve_timeout_seconds,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text, fetch_capped_rows,
    find_naming_convention_relationships,
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


def _quote_ident(name):
    """Double-quotes a Snowflake identifier for interpolation into a plain
    SQL string (escaping an embedded '"' the way Snowflake itself expects),
    for the Phase 2 per-table live-query section and the Phase 1 Grants
    section below - mirrors backends/postgres.py's own `_quote_ident`
    helper exactly, same reasoning: Snowflake's default paramstyle has no
    bind-parameter form for a bare identifier (table/column/role name), so
    a handful of genuinely dynamic statements are built as plain strings
    here rather than via a query-builder. `name` always comes from data
    this same connection already queried (kept_names / column names Phase
    1 fetched, or CURRENT_ROLE()'s own return value) - never raw user
    input - so this only needs to be correct, not defend against an
    adversarial identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def _pad_column_row(row):
    """A columns_rows tuple may still be the pre-identity/comment 4-tuple
    (table_name, column_name, data_type, is_nullable) that every test
    predating this feature already uses - padded here to the real 7-column
    shape _build_shallow_schema_parts()'s columns query now selects (...,
    is_identity, identity_generation, comment), defaulting to "NO"/None/
    None (not an identity column, no comment), so none of those existing
    tests need to be rewritten just because three more columns joined the
    SELECT list. Mirrors backends/postgres.py's own `_pad_column_row`."""
    row = list(row)
    while len(row) < 7:
        row.append("NO" if len(row) == 4 else None)
    return tuple(row)


# Phase 2 (deep-only) sampling: which information_schema.columns.data_type
# strings are worth a MIN()/MAX() range query (numeric/date-ish) vs a
# frequent-value GROUP BY (bounded/categorical-ish) - see get_schema()'s
# "Column value samples" section below. Deliberately conservative/small
# lists (Snowflake's own documented base type names), same spirit as
# backends/postgres.py's identically-named constants: a data_type this
# doesn't recognize (VARIANT/ARRAY/OBJECT/BINARY/GEOGRAPHY/...) is simply
# skipped for sampling rather than guessed at.
NUMERIC_OR_DATE_TYPES = frozenset({
    "NUMBER", "FLOAT", "DATE", "TIME",
    "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "TIMESTAMP",
})
CATEGORICAL_TYPES = frozenset({"TEXT", "VARCHAR", "CHAR", "STRING", "BOOLEAN"})

# Bounds on Phase 2's per-table sampling cost - same values as
# backends/postgres.py's identically-named constants (see that module for
# the full rationale); kept identical here for consistent behavior across
# ANSI-SQL-ish backends rather than an arbitrary per-dialect tune.
MAX_COLUMNS_FOR_SAMPLING = 25
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
FREQUENT_VALUES_LIMIT = 15


def _is_near_unique_ratio(approx_distinct, live_count):
    """Gate for whether a categorical column's "frequent values" sample is
    worth rendering at all - Snowflake has no Postgres-style pre-computed
    planner statistic (pg_stats.n_distinct) exposed via INFORMATION_SCHEMA,
    so this uses APPROX_COUNT_DISTINCT(col) (cheap, warehouse-side
    approximate cardinality - see the module docstring) divided by the same
    query's live COUNT(*), which get_schema()'s Phase 2 loop fetches
    together in one combined statement per table (see "Live row counts /
    cardinality gate" below). `None`/zero live_count is treated as "not
    near-unique" - permissive by design, matching
    backends/postgres.py's _is_near_unique_n_distinct's own treatment of an
    unknown stat, since the alternative (skipping sampling whenever the
    combined query itself failed) would silently hide sampling forever for
    a table that briefly errored."""
    if approx_distinct is None or live_count is None or live_count <= 0:
        return False
    return (approx_distinct / live_count) >= 0.5


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
            # login_timeout bounds only the connect/authenticate phase,
            # never query execution afterwards (that's network_timeout,
            # deliberately left unset here) - see backends/base.py's
            # DB_CONNECT_TIMEOUT_SECONDS docstring for why a wrong/
            # unreachable account needs to fail fast here rather than
            # hanging indefinitely.
            "login_timeout": resolve_timeout_seconds(
                descriptor, "connect_timeout_seconds", DB_CONNECT_TIMEOUT_SECONDS,
            ),
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
        PostgresBackend.cache_key's username@host:port/dbname and
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

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - see the module-level docstring and
        backends/base.py's Backend.get_schema()/get_schema_shallow()
        docstrings for why this split exists at all.

        Returns None if CURRENT_SCHEMA() has no BASE TABLE at all (mirrors
        get_schema()'s old "return None" for that case). Otherwise returns
        (schema_parts, table_columns, phase2_ctx):
          - schema_parts: the ordered list of text sections, not yet
            joined/capped.
          - table_columns: {table_name: [column_name, ...]}, scoped to the
            same bounded kept_names set - handed to the shared
            find_naming_convention_relationships() helper by get_schema()'s
            Phase 2 pass.
          - phase2_ctx: raw, already-fetched data Phase 2 reuses without
            re-querying - kept_names/column_types (for deciding what to
            sample) and the raw views/procedures/functions rows (so
            get_schema() can render full view/routine bodies without a
            second trip to the database - see the "Views"/"Routines"
            sections below for why only the *name*/*signature* is rendered
            here).
        """
        schema_parts = []
        table_columns = {}
        column_types = {}

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
            # formatted into the SQL. is_identity/identity_generation (new)
            # mark a column as an auto-generated identity column - rendered
            # as an "IDENTITY" marker on the column's own line, mirroring
            # backends/postgres.py's identical treatment. comment (new) is
            # rendered separately, in the "Comments:" section below
            # (alongside table-level comments from the table-metadata query
            # further down) rather than inline, again mirroring Postgres.
            placeholders = ", ".join(["%s"] * len(kept_names))
            cursor.execute(f"""
                SELECT table_name, column_name, data_type, is_nullable,
                       is_identity, identity_generation, comment
                FROM information_schema.columns
                WHERE table_schema = CURRENT_SCHEMA()
                  AND table_name IN ({placeholders})
                ORDER BY table_name, ordinal_position;
            """, tuple(kept_names))
            columns_data = cursor.fetchall()

            tables = {}
            column_comments = {}
            for raw_row in columns_data:
                (table_name, col_name, data_type, is_nullable,
                 is_identity, identity_generation, comment) = _pad_column_row(raw_row)
                tables.setdefault(table_name, [])
                table_columns.setdefault(table_name, []).append(col_name)
                column_types.setdefault(table_name, {})[col_name] = data_type
                if comment:
                    column_comments.setdefault(table_name, {})[col_name] = comment
                identity_str = ""
                if is_identity == "YES":
                    identity_str = (
                        f" IDENTITY ({identity_generation})" if identity_generation else " IDENTITY"
                    )
                tables[table_name].append(
                    f"  {col_name} {data_type} "
                    f"{'NULL' if is_nullable == 'YES' else 'NOT NULL'}{identity_str}"
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
            # name could ever appear in it. Shallow rendering is name-only
            # (no view_definition body) - the raw rows (including each
            # view's body) are still fetched here and threaded through via
            # phase2_ctx so get_schema() (deep) can render the full body
            # without a second query - see get_schema()'s "View
            # definitions" section.
            views = []
            try:
                cursor.execute("""
                    SELECT table_name, view_definition
                    FROM information_schema.views
                    WHERE table_schema = CURRENT_SCHEMA();
                """)
                views = cursor.fetchall()
                if views:
                    schema_parts.append(
                        "Views:\n" + "\n".join(f"  View {v[0]}" for v in views)
                    )
            except Exception:
                pass

            # 4. Table metadata (new): comment, row-count estimate, and
            # clustering key, all exposed directly on
            # information_schema.tables (no extra join/per-table function
            # call needed) - scoped to kept_names since these are all
            # per-table facts about the same bounded set of base tables
            # Phase 1 already committed to describing. CLUSTERING_KEY is
            # Snowflake's documented column for a table's explicit
            # clustering key (NULL when none is defined) - chosen over a
            # per-table SYSTEM$CLUSTERING_INFORMATION(...) table-function
            # call because it's a single query against the same view this
            # section already reads, not up to len(kept_names) extra round
            # trips, which matters a lot here since get_schema_shallow() is
            # exactly the path db.py's all-dbs triage calls for every
            # in-scope connection, not just ones actually selected.
            table_comments = {}
            row_count_estimates = {}
            clustering_keys = {}
            try:
                cursor.execute(f"""
                    SELECT table_name, comment, row_count, clustering_key
                    FROM information_schema.tables
                    WHERE table_schema = CURRENT_SCHEMA()
                      AND table_name IN ({placeholders});
                """, tuple(kept_names))
                for tbl, comment, row_count, clustering_key in cursor.fetchall():
                    if comment:
                        table_comments[tbl] = comment
                    if row_count is not None:
                        row_count_estimates[tbl] = row_count
                    if clustering_key:
                        clustering_keys[tbl] = clustering_key
            except Exception:
                pass

            comment_lines = []
            for tbl in kept_names:
                if tbl in table_comments:
                    comment_lines.append(f"  [table] {tbl}: {table_comments[tbl]}")
                for col, comment in (column_comments.get(tbl) or {}).items():
                    comment_lines.append(f"  [column] {tbl}.{col}: {comment}")
            if comment_lines:
                schema_parts.append("Comments:\n" + "\n".join(comment_lines))

            if row_count_estimates:
                rc_lines = [
                    f"  {tbl}: ~{int(row_count_estimates[tbl])} rows (estimate)"
                    for tbl in kept_names if tbl in row_count_estimates
                ]
                schema_parts.append("Row count estimates:\n" + "\n".join(rc_lines))

            if clustering_keys:
                ck_lines = [
                    f"  {tbl}: {clustering_keys[tbl]}"
                    for tbl in kept_names if tbl in clustering_keys
                ]
                schema_parts.append("Clustering keys:\n" + "\n".join(ck_lines))

            # 5. External tables (new) - like Views above, deliberately NOT
            # scoped to kept_names: the Phase 1 table-name scan filters
            # table_type = 'BASE TABLE', so an external table (table_type =
            # 'EXTERNAL TABLE') could never appear in kept_names - scoping
            # this query to it would silently return zero rows, always.
            try:
                cursor.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = CURRENT_SCHEMA()
                      AND table_type = 'EXTERNAL TABLE';
                """)
                external_tables = [row[0] for row in cursor.fetchall()]
                if external_tables:
                    schema_parts.append(
                        "External tables:\n" + "\n".join(f"  {t}" for t in external_tables)
                    )
            except Exception:
                pass

            # 6/7. Routines (new) - existence + signature only, no body
            # (see get_schema()'s "Routine definitions" section for the
            # full-body deep-only counterpart, reusing the rows fetched
            # here rather than re-querying them). Unlike Postgres/MySQL,
            # Snowflake keeps procedures and functions in two separate
            # INFORMATION_SCHEMA views (no unified ROUTINES view) -
            # ARGUMENT_SIGNATURE is Snowflake's own documented column
            # giving the full parameter-list text, so no PARAMETERS-style
            # join is needed either. Not scoped to kept_names (like Views
            # above) - routines aren't tables and CURRENT_SCHEMA() alone
            # already bounds this to the connection's own schema.
            procedures = []
            try:
                cursor.execute("""
                    SELECT procedure_name, argument_signature, data_type, procedure_definition
                    FROM information_schema.procedures
                    WHERE procedure_schema = CURRENT_SCHEMA();
                """)
                procedures = cursor.fetchall()
            except Exception:
                pass

            functions = []
            try:
                cursor.execute("""
                    SELECT function_name, argument_signature, data_type, function_definition
                    FROM information_schema.functions
                    WHERE function_schema = CURRENT_SCHEMA();
                """)
                functions = cursor.fetchall()
            except Exception:
                pass

            routine_lines = [
                f"  [procedure] {name}({sig or ''}) -> {ret_type}"
                for (name, sig, ret_type, _body) in procedures
            ] + [
                f"  [function] {name}({sig or ''}) -> {ret_type}"
                for (name, sig, ret_type, _body) in functions
            ]
            if routine_lines:
                schema_parts.append("Routines:\n" + "\n".join(routine_lines))

            # 8. Session facts (new) - one line for the whole connection,
            # not per-table. CURRENT_TIMEZONE() is the session's effective
            # timezone. Unlike Postgres's single `datcollate` database
            # setting, Snowflake has no one global default-collation fact
            # to report: collation in Snowflake is set per-column (via
            # COLLATE(...) in a column definition) or per-session
            # (COLLATION session parameter, itself rarely set), not a
            # single database-wide value the way pg_database.datcollate
            # is - so this deliberately reports timezone only rather than
            # fabricate a collation line that wouldn't describe anything
            # real.
            try:
                cursor.execute("SELECT CURRENT_TIMEZONE();")
                row = cursor.fetchone()
                if row:
                    schema_parts.append(f"Session: timezone={row[0]}")
            except Exception:
                pass

            # 9. Grants (new) - scoped to the connection's own current role
            # only ("widened current-user grants", per the plan - not a
            # full grantee matrix). Snowflake has no INFORMATION_SCHEMA
            # table-grants view the way Postgres's role_table_grants or
            # MySQL's TABLE_PRIVILEGES do; SHOW GRANTS TO ROLE <role> is
            # the documented way to list a role's effective privileges.
            # IDENTIFIER()-free string interpolation here is safe (not
            # user input) because `role` is CURRENT_ROLE()'s own return
            # value, quoted via _quote_ident exactly like a kept_names
            # table name is in the Phase 2 per-table queries below. SHOW
            # GRANTS TO ROLE's documented column order is (created_on,
            # privilege, granted_on, name, granted_to, grantee_name,
            # grant_option, granted_by) - not verified against a real
            # account (see module docstring), so this reads by position
            # rather than by cursor.description name (a real DB-API
            # description would let this be more robust, but every other
            # query in this module already relies on positional tuple
            # unpacking the same way, and description isn't populated for
            # SHOW-command shaped result sets in every driver version).
            # One line per table (privileges combined), not one line per
            # privilege - per the plan's "not a full grantee matrix"
            # guidance.
            try:
                cursor.execute("SELECT CURRENT_ROLE();")
                role_row = cursor.fetchone()
                role = role_row[0] if role_row else None
                if role:
                    cursor.execute(f"SHOW GRANTS TO ROLE {_quote_ident(role)};")
                    grant_rows = cursor.fetchall()
                    kept_upper = {n.upper() for n in kept_names}
                    grants_by_table = {}
                    for grow in grant_rows:
                        if len(grow) < 4:
                            continue
                        privilege, granted_on, name = grow[1], grow[2], grow[3]
                        if granted_on != "TABLE":
                            continue
                        simple_name = str(name).split(".")[-1].strip('"')
                        if simple_name.upper() not in kept_upper:
                            continue
                        grants_by_table.setdefault(simple_name, set()).add(privilege)
                    if grants_by_table:
                        grant_lines = [
                            f"  {tbl}: {', '.join(sorted(privs))} (role {role})"
                            for tbl, privs in sorted(grants_by_table.items())
                        ]
                        schema_parts.append("Grants (current role):\n" + "\n".join(grant_lines))
            except Exception:
                pass

            # 10. Row-level security (row access policies) / column
            # masking policies, plus external-table flag (rendered
            # separately above) (new). Snowflake's per-object attribution
            # (which table/column a given policy is actually attached to)
            # is only available via the POLICY_REFERENCES table function,
            # called once per table/column - too expensive to run
            # unconditionally here (get_schema_shallow() is the exact path
            # db.py's all-dbs triage calls for every in-scope connection,
            # so a per-kept-table function call here would multiply
            # Phase 1's query count by len(kept_names), defeating the point
            # of a cheap shallow fetch). Instead this reports existence
            # only, schema-wide, via SHOW ROW ACCESS POLICIES / SHOW
            # MASKING POLICIES - two single, cheap queries, each wrapped in
            # its own try/except since either feature (or the privilege to
            # list it) may be unavailable independently of the other.
            policy_flags = []
            try:
                cursor.execute("SHOW ROW ACCESS POLICIES IN SCHEMA;")
                if cursor.fetchall():
                    policy_flags.append("row access polic(ies) defined in this schema")
            except Exception:
                pass
            try:
                cursor.execute("SHOW MASKING POLICIES IN SCHEMA;")
                if cursor.fetchall():
                    policy_flags.append("masking polic(ies) defined in this schema")
            except Exception:
                pass
            if policy_flags:
                schema_parts.append(
                    "Row-level security / masking:\n  " + "; ".join(policy_flags)
                    + " (existence only - not attributed to specific tables/columns here)"
                )

            # Deliberately no Indexes/Triggers sections: Snowflake has no
            # user-managed indexes (automatic micro-partition pruning/
            # clustering instead - see "Clustering keys" above for the one
            # real, introspectable piece of that) and no trigger support at
            # all.

        phase2_ctx = {
            "kept_names": kept_names,
            "column_types": column_types,
            "views": views,
            "procedures": procedures,
            "functions": functions,
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
        """Deep fetch: Phase 1 (catalog-only, via
        _build_shallow_schema_parts()) plus Phase 2's live-query
        enrichment - full view/routine bodies, per-table live row counts
        with an APPROX_COUNT_DISTINCT-gated column-value-sample pass, and
        the shared naming-convention relationship heuristic. See
        get_schema_shallow() above for the catalog-only subset this builds
        on top of, and backends/base.py's Backend.get_schema() docstring
        for the two-phase rationale."""
        built = self._build_shallow_schema_parts(connection)
        if built is None:
            return None
        schema_parts, table_columns, phase2_ctx = built
        schema_parts = list(schema_parts)

        kept_names = phase2_ctx["kept_names"]
        column_types = phase2_ctx["column_types"]
        views = phase2_ctx["views"]
        procedures = phase2_ctx["procedures"]
        functions = phase2_ctx["functions"]

        # Phase 2 (deep-only): full view/routine bodies, reusing the raw
        # rows _build_shallow_schema_parts already fetched - no re-query.
        if views:
            view_lines = [f"  View {v[0]}: {(v[1] or '').strip()}" for v in views]
            # view_definition legitimately comes back NULL (not just an
            # empty string) when the connected role lacks the privilege to
            # see a given view's definition - `(v[1] or '').strip()`
            # matches every other backend's own views-section guard.
            schema_parts.append("View definitions:\n" + "\n".join(view_lines))

        routine_body_lines = [
            f"  [procedure] {name}: {(body or '').strip()}"
            for (name, _sig, _ret, body) in procedures if (body or "").strip()
        ] + [
            f"  [function] {name}: {(body or '').strip()}"
            for (name, _sig, _ret, body) in functions if (body or "").strip()
        ]
        if routine_body_lines:
            schema_parts.append("Routine definitions:\n" + "\n".join(routine_body_lines))

        with connection.cursor() as cursor:
            live_count_lines = []
            sample_blocks = []
            for table_name in kept_names:
                col_types = column_types.get(table_name) or {}
                if not col_types:
                    # A kept_names entry cap_kept_tables dropped columns
                    # for (shouldn't normally happen - kept_names and
                    # column_types are built from the same query - but
                    # guards against an empty/omitted table cleanly).
                    continue

                numeric_cols = [
                    c for c, t in col_types.items() if t in NUMERIC_OR_DATE_TYPES
                ][:MAX_NUMERIC_COLUMNS_FOR_MINMAX]
                categorical_cols = [c for c, t in col_types.items() if t in CATEGORICAL_TYPES]
                too_wide = len(col_types) > MAX_COLUMNS_FOR_SAMPLING

                # Live row count + cardinality gate, combined into one
                # query per table where there are categorical columns to
                # gate at all (APPROX_COUNT_DISTINCT alongside COUNT(*) in
                # the same SELECT - cheaper than two separate round trips,
                # since both need to scan/aggregate the same table
                # anyway). A too-wide table (or one with no categorical
                # columns) just gets the plain live count - it still needs
                # one, per the plan's "supersedes/annotates the Phase 1
                # estimate" guidance, even when sampling itself is skipped.
                live_count = None
                approx_distinct = {}
                try:
                    if too_wide or not categorical_cols:
                        cursor.execute(f"SELECT COUNT(*) FROM {_quote_ident(table_name)};")
                        row = cursor.fetchone()
                        if row is not None:
                            live_count = row[0]
                    else:
                        select_parts = ["COUNT(*)"] + [
                            f"APPROX_COUNT_DISTINCT({_quote_ident(c)})" for c in categorical_cols
                        ]
                        cursor.execute(
                            f"SELECT {', '.join(select_parts)} FROM {_quote_ident(table_name)};"
                        )
                        row = cursor.fetchone()
                        if row is not None:
                            live_count = row[0]
                            for i, c in enumerate(categorical_cols):
                                approx_distinct[c] = row[i + 1]
                except Exception:
                    pass

                if live_count is not None:
                    live_count_lines.append(f"  {table_name}: {live_count} rows (live, authoritative)")

                if too_wide:
                    # Table too wide to sample column-by-column without an
                    # explosion of tiny queries - already has its live
                    # count above, just no per-column sampling below.
                    continue

                table_sample_lines = []

                # Min/max, all eligible numeric/date columns in one
                # combined query per table (bounded by
                # MAX_NUMERIC_COLUMNS_FOR_MINMAX) rather than one query per
                # column.
                if numeric_cols:
                    try:
                        select_parts = ", ".join(
                            f'MIN({_quote_ident(c)}) AS "{c}__min", MAX({_quote_ident(c)}) AS "{c}__max"'
                            for c in numeric_cols
                        )
                        cursor.execute(f"SELECT {select_parts} FROM {_quote_ident(table_name)};")
                        row = cursor.fetchone()
                        if row is not None:
                            for i, c in enumerate(numeric_cols):
                                min_v, max_v = row[2 * i], row[2 * i + 1]
                                table_sample_lines.append(f"    {c}: range [{min_v} .. {max_v}]")
                    except Exception:
                        pass

                # Frequent values - one query per eligible categorical
                # column (capped at MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE
                # per table), skipping any column the APPROX_COUNT_DISTINCT/
                # live-count ratio above says is close to unique (see
                # _is_near_unique_ratio).
                eligible_categorical = []
                for c in categorical_cols:
                    if _is_near_unique_ratio(approx_distinct.get(c), live_count):
                        continue
                    eligible_categorical.append(c)
                    if len(eligible_categorical) >= MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE:
                        break

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
                        count = row_count if row_count is not None and row_count >= 0 else 0

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
