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
    catalog tables). This same query has since been widened (see "Row count
    estimates" below) to also select tbl_rows/stats_off - a free column
    addition to a query that already runs, not a second new query.
  - No "Triggers" section. Redshift has zero trigger support - there's
    nothing meaningful to query, so the section is omitted entirely rather
    than issuing a query against a catalog view that may not even exist.
  - Constraints (PK/FK/UNIQUE) ARE included, same query shape as Postgres,
    but Redshift - like Snowflake/Databricks - lets you declare them
    without ever enforcing them at write time; the schema text says so
    explicitly, and translate_routes.py's dialect prompt intro repeats the
    same warning, so generated SQL doesn't assume DB-enforced integrity.
  - Grants: previously deliberately omitted entirely, with a comment noting
    information_schema.role_table_grants support is inconsistent across
    Redshift versions/configurations (svv_relation_privileges may be the
    more portable source). That concern hasn't gone away - it still isn't
    verifiable from this sandbox against a real cluster - but rather than
    keep deferring it outright, a "Grants" section is now attempted the
    same try/except-wrapped, best-effort way the constraints/layout
    sections already were: if role_table_grants behaves the way it does on
    Postgres, the section renders; if it errors (unsupported view, a role
    without catalog access, an older/differently-configured cluster), the
    whole section is silently skipped, same as every other best-effort
    section here - see "5. Grants" below.

Scoped with current_schema() rather than a hardcoded 'public' the way
backends/postgres.py hardcodes it - this dialect explicitly supports a
"schema" descriptor override (see connect() above), so introspection needs
to respect whatever search_path that override left in effect, not assume
'public' unconditionally the way Postgres's simpler (no schema-override
field) descriptor can get away with.

Two-phase split (get_schema_shallow() / get_schema()): see backends/base.py's
Backend.get_schema()/get_schema_shallow() docstrings for the general shape.
_build_shallow_schema_parts() below runs every Phase 1 (catalog-only) query
once; get_schema_shallow() joins/caps that directly, and get_schema() (deep)
appends Phase 2's live-query sections (full view/routine body text, per-table
sampling, live row counts, the naming-convention pass) before joining/capping
once. New Phase 1 additions on top of the original tables/columns/
constraints/layout/views:
  - Identity-column marker: NOT sourced from information_schema.columns'
    is_identity/identity_generation the way backends/postgres.py's does.
    Those two columns are a PostgreSQL 10+ addition (GENERATED ALWAYS AS
    IDENTITY) that Redshift's own information_schema fork - based on an
    older Postgres catalog snapshot - does not carry, and Redshift's own
    `IDENTITY(seed, step)` column syntax predates and is unrelated to that
    Postgres feature entirely. Instead, a Redshift IDENTITY column's
    "default" (already selected by the existing columns query - no new
    query needed) is Redshift's own internal identity-default expression
    (documented shape: something like `"identity"(<seed>, 0, '<seed>,<step>'
    ::text)`), so _identity_marker_from_column_default() below parses that
    text out of column_default instead - see its own docstring for exactly
    what it recognizes and how it degrades when the internal format doesn't
    match what's documented.
  - Table/column comments: pg_description, queried directly (not via
    obj_description()/col_description() the way backends/postgres.py does)
    - those two are convenience wrapper functions, and Redshift's documented
    list of supported PostgreSQL functions is narrower than Postgres's own,
    so querying the underlying pg_description/pg_class/pg_attribute/
    pg_namespace catalog tables directly (which Redshift, as a Postgres
    fork, does carry) is the safer bet here - wrapped in the same
    try/except as everywhere else regardless.
  - Row count estimate: svv_table_info's existing DISTSTYLE/SORTKEY query
    (see above) widened to also select tbl_rows/stats_off - both real
    svv_table_info columns - so no second query is needed. stats_off > 0
    means the estimate may be stale (Redshift's own definition: the
    percentage difference between current table stats and actual table
    data since the last ANALYZE), rendered as an explicit caveat rather
    than a bare, potentially-misleading number.
  - Routines: attempted via the same information_schema.routines/parameters
    shape backends/postgres.py uses, wrapped in try/except. Whether
    Redshift's dialect actually supports this well for user-defined
    functions/stored procedures could not be verified from this sandbox (no
    live cluster reachable) - rather than skip it outright on suspicion
    alone, or force it uncritically, it's attempted and left to degrade
    silently (empty Routines section, no error) if the real shape differs
    on a given cluster/version - mirrors this file's own established
    Grants-style caution.
  - Session facts: current_setting('TimeZone') - identical mechanism to
    backends/postgres.py's own (Redshift is Postgres-derived here too).
  - Grants: see above.
  - External tables: svv_external_tables (Redshift Spectrum) - flagged as
    an informational section, not scoped to kept_names (an external table
    is never a BASE TABLE in the connected schema's own information_schema.
    tables, so it could never appear in kept_names anyway - same reasoning
    backends/postgres.py's own Views section relies on for why it isn't
    scoped to kept_names either). Row-level security is NOT a Redshift
    concept (see backends/postgres.py's own pg_policies/relrowsecurity
    section, which has no Redshift equivalent) - deliberately not
    fabricated here.

NOTE for reviewers: like backends/snowflake.py/backends/databricks.py/
backends/oracle.py, this has been exercised against the fake DB-API harness
in tests/server/helpers.py (reusing the existing Postgres psycopg2 fake,
since the driver is identical), not a real Redshift cluster yet - treat the
SVV_TABLE_INFO query and every new best-effort section above as solid first
drafts to validate before relying on them against a real cluster.
"""

import logging
import re

import psycopg2
from psycopg2 import sql
import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS, resolve_timeout_seconds,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text, fetch_capped_rows,
    find_naming_convention_relationships, min_frequent_value_count, FREQUENT_VALUES_LIMIT,
    format_dataset_size_line, format_multiline_schema_entry_body,
)


def _quote_ident(name):
    """Double-quotes a Redshift/Postgres-wire identifier for interpolation
    into a plain SQL string (escaping an embedded '"' the way Postgres
    itself expects) - used by the Phase 2 per-table live-query section
    below (get_schema()'s sampling/live-count queries), where `name` always
    comes from information_schema data this same connection already
    queried (kept_names/column names Phase 1 just fetched), never from raw
    user input. Same helper/reasoning as backends/postgres.py's own
    _quote_ident()."""
    return '"' + str(name).replace('"', '""') + '"'


# Phase 2 (deep-only) sampling: which information_schema.columns.data_type
# strings are worth a MIN()/MAX() range query (numeric/date-ish) vs a
# frequent-value GROUP BY (bounded/categorical-ish) - mirrors
# backends/postgres.py's own NUMERIC_OR_DATE_TYPES/CATEGORICAL_TYPES lists,
# minus "uuid" (Redshift has no native UUID column type - a UUID-shaped
# column here is just a "character varying" column, already covered by that
# entry) and the "time [with/without] time zone" entries (Redshift's data
# types are otherwise the same Postgres-derived names). Deliberately a
# conservative/small allowlist, not "everything else" - see
# backends/postgres.py's own comment for why an unrecognized data_type is
# just skipped for sampling rather than guessed at.
NUMERIC_OR_DATE_TYPES = frozenset({
    "smallint", "integer", "bigint", "decimal", "numeric", "real", "double precision",
    "date", "timestamp without time zone", "timestamp with time zone",
})
CATEGORICAL_TYPES = frozenset({"character varying", "character", "text", "boolean"})

_logger = logging.getLogger(__name__)

# Bounds on Phase 2's per-table sampling cost - identical values to
# backends/postgres.py's own (see that module for the full rationale each
# bound guards against; repeated here rather than imported so this file has
# no runtime dependency on backends/postgres.py, only a documented shared
# convention).
MAX_COLUMNS_FOR_SAMPLING = 25
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
# FREQUENT_VALUES_LIMIT now imported from backends/base.py (env-configurable
# via SCHEMA_FREQUENT_VALUES_LIMIT) rather than defined here - see that
# module's own comment.


def _is_near_unique_n_distinct(n_distinct):
    """Same gate as backends/postgres.py's own _is_near_unique_n_distinct()
    - Redshift shares Postgres's pg_stats catalog view (and its
    n_distinct column's documented meaning: non-negative = absolute
    estimated distinct count, negative = -(distinct/rows) ratio), so the
    identical logic applies unchanged. See that module's docstring for the
    full reasoning; not imported from there so this file has no runtime
    dependency on backends/postgres.py."""
    if n_distinct is None:
        return False
    if n_distinct < 0:
        return n_distinct <= -0.5
    return n_distinct > 1000


# Matches a "'<seed>,<step>'"-shaped substring inside Redshift's own
# internal identity-default expression text - see
# _identity_marker_from_column_default()'s docstring for exactly where this
# is used and why it's best-effort.
_REDSHIFT_IDENTITY_SEED_STEP_RE = re.compile(r"'(-?\d+)\s*,\s*(-?\d+)'")


def _identity_marker_from_column_default(column_default):
    """Best-effort IDENTITY marker for a Redshift column, parsed from
    `column_default` (already selected by the existing columns query below
    - no new query needed for this).

    Redshift's own `IDENTITY(seed, step)` column syntax is a completely
    different mechanism from Postgres's `GENERATED ALWAYS AS IDENTITY` -
    see this module's own docstring for why information_schema.columns'
    is_identity/identity_generation (what backends/postgres.py reads)
    genuinely does not reflect it on Redshift. What Redshift *does* expose
    for an identity column is its internal identity-default expression via
    column_default - documented (and observed in the wild) to look
    something like `"identity"(<seed>, 0, '<seed>,<step>'::text)`. This
    could not be verified against a live cluster from this sandbox, so
    parsing here is deliberately conservative: any column_default whose
    text contains "identity" (case-insensitive) is marked as an identity
    column at all; a "'<seed>,<step>'"-shaped substring inside it (as in
    the documented shape above) is additionally extracted and rendered as
    seed/step, but a column_default that says "identity" without matching
    that inner shape (e.g. a differently-formatted internal representation
    on some cluster/version this sandbox couldn't check) still gets a bare
    "IDENTITY" marker rather than being silently missed.

    Returns "" for a plain (non-identity) default/no default at all."""
    if not column_default:
        return ""
    if "identity" not in column_default.lower():
        return ""
    match = _REDSHIFT_IDENTITY_SEED_STEP_RE.search(column_default)
    if match:
        return f" IDENTITY(seed={match.group(1)}, step={match.group(2)})"
    return " IDENTITY"


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
        # Wrapped in int(round(...)) because libpq's connect_timeout is a
        # strict integer connection option - see backends/postgres.py's
        # identical connect_timeout kwarg for the full explanation (this
        # dialect shares psycopg2/libpq underneath, and shares the exact
        # same bug a real user hit: an unrounded float override like 60.0
        # stringifies to "60.0", which libpq rejects outright as an
        # "invalid integer value" before ever dialing out).
        connection = psycopg2.connect(
            host=host, port=port, dbname=database, user=user, password=password,
            sslmode="require", connect_timeout=int(round(resolve_timeout_seconds(
                descriptor, "connect_timeout_seconds", DB_CONNECT_TIMEOUT_SECONDS,
            ))),
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

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - see the module docstring's two-phase
        section and backends/base.py's Backend.get_schema()/
        get_schema_shallow() docstrings for why this split exists.

        Returns None if the connection's current_schema() has no BASE TABLE
        at all (mirrors get_schema()'s old "return None" for that case).
        Otherwise returns (schema_parts, table_columns, phase2_ctx) - same
        three-part shape as backends/postgres.py's own
        _build_shallow_schema_parts(); see that module's docstring for what
        each element is for. phase2_ctx here carries kept_names/
        column_types (for deciding what Phase 2 samples) and the raw views/
        routines rows (so get_schema() can render their full body text
        without a second trip to the database).
        """
        schema_parts = []
        table_columns = {}
        column_types = {}

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
            # column_default is also used below to derive the IDENTITY
            # marker (see _identity_marker_from_column_default) - no extra
            # column/query needed for that, unlike backends/postgres.py's
            # own is_identity/identity_generation addition (which genuinely
            # doesn't apply here - see module docstring).
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
                table_columns.setdefault(table_name, []).append(col_name)
                column_types.setdefault(table_name, {})[col_name] = data_type
                identity_str = _identity_marker_from_column_default(col_default)
                # Suppress the raw DEFAULT clause when it's really an
                # internal identity-default expression (e.g.
                # "identity"(387363, 0, '1,1'::text)) - not useful SQL to
                # show verbatim once the human-readable IDENTITY marker
                # above already says the same thing more clearly.
                default_str = f" DEFAULT {col_default}" if col_default and not identity_str else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                tables.setdefault(table_name, []).append(
                    f"  {col_name} {data_type} {null_str}{default_str}{identity_str}"
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
            # same reasoning as the constraints query above. Widened (new)
            # to also select tbl_rows/stats_off - both real svv_table_info
            # columns - for the "Row count estimates" section below, a free
            # column addition to a query that already runs rather than a
            # second new query.
            try:
                cursor.execute("""
                    SELECT "table", diststyle, sortkey1, tbl_rows, stats_off
                    FROM svv_table_info
                    WHERE schema = current_schema()
                      AND "table" = ANY(%s);
                """, (kept_names,))
                layout_rows = cursor.fetchall()
                layout_lines = []
                row_count_lines = []
                for tbl, diststyle, sortkey1, tbl_rows, stats_off in layout_rows:
                    parts = []
                    if diststyle:
                        parts.append(f"DISTSTYLE {diststyle}")
                    if sortkey1:
                        parts.append(f"SORTKEY({sortkey1})")
                    if parts:
                        layout_lines.append(f"  [{tbl}] {', '.join(parts)}")
                    if tbl_rows is not None:
                        # stats_off is Redshift's own documented "percent
                        # difference between current stats and actual table
                        # data" - >0 means the tbl_rows estimate below may
                        # be stale (an ANALYZE is due), so that's surfaced
                        # as an explicit caveat rather than a bare number
                        # that looks more authoritative than it is.
                        staleness = (
                            f" (stats may be stale - {stats_off}% off since last ANALYZE)"
                            if stats_off is not None and stats_off > 0 else ""
                        )
                        row_count_lines.append(f"  {tbl}: ~{int(tbl_rows)} rows (estimate){staleness}")
                if layout_lines:
                    schema_parts.append(
                        "Distribution/Sort Keys (Redshift has no index concept - this is what "
                        "actually drives query performance, not an index list):\n"
                        + "\n".join(layout_lines)
                    )
                if row_count_lines:
                    schema_parts.append("Row count estimates:\n" + "\n".join(row_count_lines))
            except Exception as exc:
                # Silently degrading (no Distribution/Sort Keys, no Row
                # count estimates - both come from this one query) is the
                # right behavior for the user, but silently losing the
                # REASON is not: svv_table_info is a Redshift SYS view that
                # defaults to superuser-only visibility - an ordinary user
                # needs an explicit "GRANT SELECT ON svv_table_info TO
                # <user>;" (or membership in a group that has it) even
                # though they can already SELECT from their own tables
                # directly. That's the most likely cause whenever this
                # query fails for an otherwise-working connection.
                _logger.warning(
                    "Redshift svv_table_info query failed for the current "
                    "schema - the connected user most likely lacks SELECT on "
                    "svv_table_info itself (this system view defaults to "
                    "superuser-only visibility in Redshift; it needs an "
                    "explicit GRANT SELECT ON svv_table_info TO <user>, "
                    "separate from ordinary table read access): %s",
                    exc,
                )

            # 4. Views - deliberately NOT scoped to kept_names, same
            # reasoning as every other backend here: that set is built
            # exclusively from BASE TABLE names, so no view name could ever
            # appear in it.
            #
            # Shallow rendering is name-only (no view_definition body) - the
            # raw rows (including each view's body) are still fetched here
            # (one query, reused by both phases) and threaded through via
            # phase2_ctx below so get_schema() (deep) can render the full
            # body without a second query - see get_schema()'s "View
            # definitions" section.
            cursor.execute("""
                SELECT
                    table_name,
                    view_definition
                FROM information_schema.views
                WHERE table_schema = current_schema();
            """)
            views = cursor.fetchall()
            if views:
                view_lines = [f"  View {v[0]}" for v in views]
                schema_parts.append("Views:\n" + "\n".join(view_lines))

            # 5. Comments (new) - table and column comments via pg_description
            # directly (not obj_description()/col_description() - see
            # module docstring for why). Best-effort like every new
            # optional section below: a role/cluster where this doesn't
            # behave still gets every other section.
            try:
                cursor.execute("""
                    SELECT * FROM (
                        SELECT c.relname AS table_name, NULL::text AS column_name,
                               d.description AS comment
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0
                        WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
                        UNION ALL
                        SELECT c.relname, a.attname, d.description
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        JOIN pg_attribute a ON a.attrelid = c.oid
                          AND a.attnum > 0 AND NOT a.attisdropped
                        LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
                        WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
                    ) sub
                    ORDER BY table_name, column_name NULLS FIRST;
                """, (kept_names, kept_names))
                comment_lines = []
                for tbl, col, comment in cursor.fetchall():
                    if not comment:
                        continue
                    if col:
                        comment_lines.append(f"  [column] {tbl}.{col}: {comment}")
                    else:
                        comment_lines.append(f"  [table] {tbl}: {comment}")
                if comment_lines:
                    schema_parts.append("Comments:\n" + "\n".join(comment_lines))
            except Exception:
                pass

            # 6. Routines (new) - existence + signature only, no body (see
            # get_schema()'s "Routine definitions" section for the
            # full-body deep-only counterpart, reusing routine_definition
            # fetched here rather than re-querying it). Same
            # information_schema.routines/parameters shape
            # backends/postgres.py uses - whether Redshift's dialect
            # supports this well for UDFs/stored procedures could not be
            # verified against a live cluster from this sandbox, so this is
            # attempted rather than assumed and left to degrade silently
            # (empty Routines section) if it doesn't hold up on a real
            # cluster - see module docstring.
            routines = []
            try:
                cursor.execute("""
                    SELECT r.routine_name,
                           COALESCE(string_agg(
                               p.parameter_name || ' ' || p.data_type, ', '
                               ORDER BY p.ordinal_position
                           ), '') AS signature,
                           r.data_type AS return_type,
                           r.routine_definition
                    FROM information_schema.routines r
                    LEFT JOIN information_schema.parameters p
                      ON p.specific_schema = r.specific_schema
                     AND p.specific_name = r.specific_name
                    WHERE r.specific_schema = current_schema()
                    GROUP BY r.routine_name, r.specific_name, r.data_type, r.routine_definition
                    ORDER BY r.routine_name;
                """)
                routines = cursor.fetchall()
                if routines:
                    routine_lines = [f"  {r[0]}({r[1]}) -> {r[2]}" for r in routines]
                    schema_parts.append("Routines:\n" + "\n".join(routine_lines))
            except Exception:
                pass

            # 7. Session timezone (new) - one line for the whole connection,
            # not per-table. current_setting('TimeZone') is identical to
            # backends/postgres.py's own (Redshift is Postgres-derived
            # here too).
            try:
                cursor.execute("SELECT current_setting('TimeZone');")
                row = cursor.fetchone()
                if row and row[0]:
                    schema_parts.append(f"Session: timezone={row[0]}")
            except Exception:
                pass

            # 8. Grants (new) - see module docstring for why this used to
            # be deliberately omitted outright, and why it's now attempted
            # (try/except-wrapped, best-effort) instead: the underlying
            # concern (role_table_grants support varies across Redshift
            # versions/configurations) hasn't gone away, so a cluster/
            # version where this errors just loses this one section, not
            # the whole schema fetch.
            try:
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
                    schema_parts.append(
                        "Grants (best-effort - role_table_grants support varies across "
                        "Redshift versions/configurations):\n" + "\n".join(grant_lines)
                    )
            except Exception:
                pass

            # 9. External tables (new) - Redshift Spectrum tables, via
            # svv_external_tables. Not scoped to kept_names - an external
            # table is never a BASE TABLE in the connected schema's own
            # information_schema.tables, so it could never appear in
            # kept_names anyway (same reasoning as the Views section
            # above). RLS is deliberately NOT covered here - it's not a
            # Redshift concept at all (see module docstring), unlike
            # backends/postgres.py's own pg_policies/relrowsecurity
            # section.
            try:
                cursor.execute("""
                    SELECT tablename
                    FROM svv_external_tables
                    WHERE schemaname = current_schema();
                """)
                external_tables = cursor.fetchall()
                if external_tables:
                    ext_lines = [f"  {row[0]}: [external table - Redshift Spectrum]" for row in external_tables]
                    schema_parts.append("External tables (Redshift Spectrum):\n" + "\n".join(ext_lines))
            except Exception:
                pass

            # Deliberately no Indexes/Triggers sections - see module
            # docstring for why (no such concept on Redshift at all).

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

        # Dataset size summary (new) - schema-wide totals, unlike the
        # per-table "Row count estimates" (Phase 1, kept_names-only,
        # svv_table_info tbl_rows estimate) and "Live row counts" (Phase 2
        # below, kept_names-only, authoritative but a live COUNT(*) per
        # table) sections nearby: kept_names is a capped subset of this
        # schema's tables (see cap_kept_tables), so neither of those
        # reflects the database's true overall size. This queries
        # svv_table_info once for every table in current_schema() - still
        # just a free catalog/statistics lookup, never a live scan - to
        # give the LLM a sense of overall scale (row count, storage, table
        # count) even when most tables were capped out of the detailed
        # sections below. svv_table_info's `size` column is in 1MB blocks,
        # hence the *1024*1024 to get a byte count for format_dataset_size_line.
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT SUM(tbl_rows), SUM(CAST(size AS BIGINT)) * 1024 * 1024, COUNT(*)
                    FROM svv_table_info
                    WHERE schema = current_schema();
                """)
                row = cursor.fetchone()
                if row is not None:
                    total_rows, total_bytes, table_count = row
                    if table_count and table_count > 0:
                        size_line = format_dataset_size_line(
                            total_rows=total_rows, total_bytes=total_bytes,
                        )
                        if size_line:
                            schema_parts.append(size_line)
        except Exception as exc:
            # See the Phase 1 svv_table_info query's own comment (above, in
            # _build_shallow_schema_parts) for why this is logged rather
            # than swallowed silently - same underlying cause (missing
            # SELECT on svv_table_info, which defaults to superuser-only
            # visibility in Redshift), just aggregated dataset-wide here
            # instead of per kept table.
            _logger.warning(
                "Redshift svv_table_info dataset-size query failed for the "
                "current schema - the connected user most likely lacks "
                "SELECT on svv_table_info itself (this system view defaults "
                "to superuser-only visibility in Redshift; it needs an "
                "explicit GRANT SELECT ON svv_table_info TO <user>, separate "
                "from ordinary table read access): %s",
                exc,
            )

        # Phase 2 (deep-only): full view/routine bodies, reusing the raw
        # rows _build_shallow_schema_parts already fetched - no re-query.
        if views:
            view_lines = [f"  View {v[0]}: {format_multiline_schema_entry_body(v[1])}" for v in views]
            # view_definition legitimately comes back NULL (not just an
            # empty string) when the connected role lacks the privilege to
            # see a given view's definition - (v[1] or '').strip() matches
            # every other backend's own views-section guard.
            schema_parts.append("View definitions:\n" + "\n".join(view_lines))

        routine_body_lines = [
            f"  {r[0]}: {format_multiline_schema_entry_body(r[3])}" for r in routines if (r[3] or "").strip()
        ]
        if routine_body_lines:
            schema_parts.append("Routine definitions:\n" + "\n".join(routine_body_lines))

        with connection.cursor() as cursor:
            # Cardinality gate for the frequent-value sampling below -
            # pg_stats.n_distinct is a planner statistic Redshift shares
            # with Postgres (no live scan) - see
            # _is_near_unique_n_distinct()'s docstring.
            distinct_stats = {}
            try:
                cursor.execute("""
                    SELECT tablename, attname, n_distinct
                    FROM pg_stats
                    WHERE schemaname = current_schema()
                      AND tablename = ANY(%s);
                """, (kept_names,))
                for tbl, col, n_distinct in cursor.fetchall():
                    distinct_stats.setdefault(tbl, {})[col] = n_distinct
            except Exception:
                pass

            live_count_lines = []
            sample_blocks = []
            for table_name in kept_names:
                col_types = column_types.get(table_name) or {}
                if not col_types:
                    continue

                # Fresh/live row count - authoritative, unlike the Phase 1
                # tbl_rows estimate above (free, but only as fresh as the
                # last ANALYZE - see the "Row count estimates" section's
                # stats_off staleness caveat). Kept as `row_count` - see
                # postgres.py's own identical comment on this - so the
                # "frequent values" sampling further down can size its
                # min_frequent_value_count() floor off the same number.
                row_count = None
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {_quote_ident(table_name)};")
                    row = cursor.fetchone()
                    if row is not None:
                        row_count = row[0]
                        live_count_lines.append(f"  {table_name}: {row_count} rows (live, authoritative)")
                except Exception:
                    pass

                if len(col_types) > MAX_COLUMNS_FOR_SAMPLING:
                    # Table too wide to sample column-by-column without an
                    # explosion of tiny queries - still gets its live count
                    # above, just no per-column sampling below.
                    continue

                numeric_cols = [
                    c for c, t in col_types.items() if t in NUMERIC_OR_DATE_TYPES
                ][:MAX_NUMERIC_COLUMNS_FOR_MINMAX]
                categorical_cols = [c for c, t in col_types.items() if t in CATEGORICAL_TYPES]

                table_sample_lines = []

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

                eligible_categorical = []
                for c in categorical_cols:
                    n_distinct = distinct_stats.get(table_name, {}).get(c)
                    if _is_near_unique_n_distinct(n_distinct):
                        continue
                    eligible_categorical.append(c)
                    if len(eligible_categorical) >= MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE:
                        break

                # HAVING floor - see postgres.py's own identical comment and
                # min_frequent_value_count()'s docstring (backends/base.py).
                min_count = min_frequent_value_count(row_count)
                having_clause = f"HAVING COUNT(*) >= {min_count} " if min_count is not None else ""
                for c in eligible_categorical:
                    try:
                        cursor.execute(
                            f"SELECT {_quote_ident(c)}, COUNT(*) FROM {_quote_ident(table_name)} "
                            f"GROUP BY {_quote_ident(c)} {having_clause}"
                            f"ORDER BY COUNT(*) DESC LIMIT {FREQUENT_VALUES_LIMIT};"
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

        # Naming-convention relationship pass (shared helper, no new SQL).
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
