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
    DB_CONNECT_TIMEOUT_SECONDS, resolve_timeout_seconds, materialize_ca_cert_tempfile,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text, fetch_capped_rows,
    find_naming_convention_relationships,
)


def _quote_ident(name):
    """Double-quotes a Postgres identifier for interpolation into a plain
    SQL string (escaping an embedded '"' the way Postgres itself expects),
    for the Phase 2 per-table live-query section below (backends/postgres.py's
    connect() already has a psycopg2.sql.Identifier-based equivalent for a
    single SET statement, but the Phase 2 queries below build a handful of
    genuinely dynamic per-table/per-column statements where a plain string
    keeps the code readable rather than composing psycopg2.sql.Composed
    trees). `name` always comes from information_schema/pg_catalog data this
    same connection already queried (kept_names / column names Phase 1 just
    fetched) - never from raw user input - so this only needs to be correct,
    not defend against adversarial identifiers."""
    return '"' + str(name).replace('"', '""') + '"'


# Phase 2 (deep-only) sampling: which information_schema.columns.data_type
# strings are worth a MIN()/MAX() range query (numeric/date-ish) vs a
# frequent-value GROUP BY (bounded/categorical-ish) - see get_schema()'s
# "Column value samples" section below. Deliberately conservative/small
# lists rather than "everything that isn't the other list" - a data_type
# this doesn't recognize (e.g. "jsonb", "bytea", an array type, a custom
# domain/enum) is simply skipped for sampling rather than guessed at, since
# an ill-fitting MIN()/MAX() or GROUP BY on the wrong shape of column is
# more likely to error or produce noise than a useful hint.
NUMERIC_OR_DATE_TYPES = frozenset({
    "smallint", "integer", "bigint", "decimal", "numeric", "real", "double precision",
    "date", "timestamp without time zone", "timestamp with time zone",
    "time without time zone", "time with time zone",
})
CATEGORICAL_TYPES = frozenset({"character varying", "character", "text", "boolean", "uuid"})

# Bounds on Phase 2's per-table sampling cost, all deliberately small - see
# get_schema()'s "Column value samples" section for how each is used.
# MAX_COLUMNS_FOR_SAMPLING: a table with more columns than this is skipped
# for sampling entirely (still gets a live row count) - a very wide table
# sampled column-by-column is exactly the "explosion of tiny queries" this
# guards against.
MAX_COLUMNS_FOR_SAMPLING = 25
# MAX_NUMERIC_COLUMNS_FOR_MINMAX: numeric/date columns beyond this many (in
# column order) are left out of the single combined MIN()/MAX() query for a
# table, bounding how wide that one query's SELECT list can get.
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
# MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE: at most this many categorical
# columns (in column order, after the pg_stats near-unique gate below) get
# their own "frequent values" GROUP BY query per table - each is a separate
# query (unlike the combined MIN()/MAX() query), so this is what actually
# bounds query count on a table with many text-ish columns.
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
# Matches the "GROUP BY ... ORDER BY COUNT(*) DESC LIMIT 15" shape called
# for in the plan this implements.
FREQUENT_VALUES_LIMIT = 15


def _is_near_unique_n_distinct(n_distinct):
    """Gate for whether a categorical column's "frequent values" sample is
    worth rendering at all, using Postgres's own already-computed planner
    statistic (`pg_stats.n_distinct`) instead of a live COUNT(DISTINCT ...)
    scan - see the plan's Phase 2 "Cardinality/distinct-value gating" line.
    Per Postgres's own documented meaning for this column: a non-negative
    value is an absolute estimated distinct-value count; a negative value is
    the negative of the ratio of distinct values to total rows (e.g. -0.9
    means ~90% of rows have a distinct value - i.e. the column is close to
    unique). `None` (no ANALYZE has ever run for this column) is treated as
    "not near-unique" - permissive by design, since the alternative (always
    skipping an unanalyzed column) would silently hide sampling for a
    freshly created table forever, and pg_stats itself may never populate on
    a role that lacks SELECT on the underlying table anyway (in which case
    the query below already returns nothing for it)."""
    if n_distinct is None:
        return False
    if n_distinct < 0:
        return n_distinct <= -0.5
    return n_distinct > 1000


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
        # resolve_timeout_seconds() lets this preset/custom connection's own
        # "connect_timeout_seconds" override the shared default - see that
        # function's docstring. Wrapped in int(round(...)) because libpq's
        # connect_timeout is a strict integer connection option - psycopg2
        # stringifies whatever's passed here straight into the DSN, and a
        # float override (resolve_timeout_seconds() always returns one when
        # a per-dataset override is actually set) produces a string like
        # "60.0", which libpq rejects outright with "invalid integer value
        # ... for connection option \"connect_timeout\"" before ever
        # attempting to dial out - a real bug this app shipped and a real
        # user hit. The no-override path was never affected: it returns
        # DB_CONNECT_TIMEOUT_SECONDS (already an int) unchanged.
        kwargs = {"connect_timeout": int(round(resolve_timeout_seconds(
            descriptor, "connect_timeout_seconds", DB_CONNECT_TIMEOUT_SECONDS,
        )))}

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
        for the *other* server's /api/translate calls too, indefinitely
        (schema_cache.py has no TTL/expiry at all - see its own module
        docstring), not just for some bounded window. Username is still
        included too (not redundant with host:port/dbname): two different
        users against the exact same database can legitimately see
        different information_schema results if their grants differ, so a
        schema fetched as one user must not be served back for another.
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

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - see the module-level docstring's note on
        the two-phase split and backends/base.py's Backend.get_schema()/
        get_schema_shallow() docstrings for why this split exists at all.

        Returns None if the connection's current_schema() has no BASE TABLE
        at all (mirrors get_schema()'s old "return None" for that case).
        Otherwise returns (schema_parts, table_columns, phase2_ctx):
          - schema_parts: the ordered list of text sections, not yet joined/
            capped - identical in kind to what get_schema() used to build
            directly, just returned before the final cap_schema_text() call.
          - table_columns: {table_name: [column_name, ...]}, scoped to the
            same bounded kept_names set schema_parts describes - handed to
            the shared find_naming_convention_relationships() helper by
            get_schema()'s Phase 2 pass (no extra query needed for that).
          - phase2_ctx: a dict of raw, already-fetched data Phase 2 wants to
            reuse without re-querying - kept_names/column_types (for
            deciding what to sample), and the raw views/routines rows (so
            get_schema() can render their full body text without a second
            trip to the database; see the "Views"/"Routines" sections below
            for why only the *name* is rendered here).
        """
        schema_parts = []
        table_columns = {}
        column_types = {}

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
            # is_identity/identity_generation (new) mark a column as an
            # auto-generated identity column (Postgres's modern replacement
            # for the old serial/sequence-default pattern) - see the
            # "IDENTITY" marker appended to each column's rendered line
            # below.
            cursor.execute("""
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.is_nullable,
                    c.column_default,
                    c.is_identity,
                    c.identity_generation
                FROM information_schema.columns c
                WHERE c.table_schema = current_schema()
                  AND c.table_name = ANY(%s)
                ORDER BY c.table_name, c.ordinal_position;
            """, (kept_names,))
            columns_data = cursor.fetchall()

            tables = {}
            for row in columns_data:
                (table_name, col_name, data_type, is_nullable,
                 col_default, is_identity, identity_generation) = row
                tables.setdefault(table_name, [])
                table_columns.setdefault(table_name, []).append(col_name)
                column_types.setdefault(table_name, {})[col_name] = data_type
                default_str = f" DEFAULT {col_default}" if col_default else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                identity_str = ""
                if is_identity == "YES":
                    identity_str = (
                        f" IDENTITY ({identity_generation})" if identity_generation else " IDENTITY"
                    )
                tables[table_name].append(
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

            # 7. Comments (new) - table and column comments via Postgres's
            # own catalog-description functions. Best-effort/try-except,
            # like every new optional section below (mirrors
            # backends/bigquery.py's own try/except-guarded optional
            # sections): a role that somehow can't evaluate these still
            # gets every other section, rather than losing the whole
            # schema fetch over one cosmetic addition.
            try:
                cursor.execute("""
                    SELECT * FROM (
                        SELECT c.relname AS table_name, NULL::text AS column_name,
                               obj_description(c.oid, 'pg_class') AS comment
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
                        UNION ALL
                        SELECT c.relname, a.attname, col_description(c.oid, a.attnum)
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        JOIN pg_attribute a ON a.attrelid = c.oid
                          AND a.attnum > 0 AND NOT a.attisdropped
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

            # 8. Row count estimate (new) - pg_class.reltuples, a free
            # planner statistic (last ANALYZE's estimate, not a live scan -
            # see get_schema()'s "Live row counts" section for the
            # authoritative, deep-only counterpart). A never-analyzed table
            # reports -1 here (or NULL on very old server versions this app
            # doesn't otherwise support) - skipped rather than shown as a
            # misleading "~-1 rows".
            try:
                cursor.execute("""
                    SELECT c.relname, c.reltuples
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema() AND c.relname = ANY(%s);
                """, (kept_names,))
                estimate_lines = []
                for tbl, reltuples in cursor.fetchall():
                    if reltuples is None or reltuples < 0:
                        continue
                    estimate_lines.append(f"  {tbl}: ~{int(round(reltuples))} rows (estimate)")
                if estimate_lines:
                    schema_parts.append("Row count estimates:\n" + "\n".join(estimate_lines))
            except Exception:
                pass

            # 9. Routines (new) - existence + signature only, no body (see
            # get_schema()'s "Routine definitions" section for the full-body
            # deep-only counterpart, reusing routine_definition fetched here
            # rather than re-querying it). Not scoped to kept_names (like
            # Views above) - routines aren't tables and current_schema()
            # alone already bounds this to the connection's own schema.
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

            # 10. Session timezone / default collation (new) - one line for
            # the whole connection, not per-table. current_setting('TimeZone')
            # is the session's effective timezone (what TIMESTAMP WITHOUT
            # TIME ZONE arithmetic and now()/CURRENT_TIMESTAMP resolve
            # against); the database's datcollate is its default collation
            # (affects text ordering/comparison).
            try:
                cursor.execute("""
                    SELECT current_setting('TimeZone'),
                           (SELECT datcollate FROM pg_database WHERE datname = current_database());
                """)
                row = cursor.fetchone()
                if row:
                    tz, collation = row[0], row[1]
                    schema_parts.append(f"Session: timezone={tz}; default collation={collation}")
            except Exception:
                pass

            # 11. RLS / federation flags (new) - row-level security (with a
            # note when it's enabled but has no policies defined, which
            # means "deny all" rather than "no restriction") and foreign
            # tables (relkind='f' - an external/federated table, e.g. via
            # postgres_fdw). Deliberately silent (no line at all) for a
            # table where every flag is false/absent, per the plan's
            # "don't render anything ... to avoid noise" instruction.
            try:
                cursor.execute("""
                    SELECT c.relname, c.relrowsecurity, c.relkind,
                           EXISTS (
                               SELECT 1 FROM pg_policies p
                               WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                           ) AS has_policies
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema() AND c.relname = ANY(%s);
                """, (kept_names,))
                rls_lines = []
                for tbl, rls_enabled, relkind, has_policies in cursor.fetchall():
                    annotations = []
                    if rls_enabled:
                        annotations.append(
                            "[RLS enabled, no policies - effectively deny-all]"
                            if not has_policies else "[RLS enabled]"
                        )
                    if relkind == 'f':
                        annotations.append("[foreign table]")
                    if annotations:
                        rls_lines.append(f"  {tbl}: {' '.join(annotations)}")
                if rls_lines:
                    schema_parts.append("Row-level security / federation:\n" + "\n".join(rls_lines))
            except Exception:
                pass

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
            # view_definition legitimately comes back NULL from Postgres
            # (not just an empty string) when the connected role lacks the
            # privilege to see a given view's definition - `(v[1] or
            # '').strip()` matches every other backend's own views-section
            # guard (see the historical note this replaces, still true
            # here: a bare v[1].strip() would raise AttributeError on that
            # None and abort schema fetch for the WHOLE database).
            schema_parts.append("View definitions:\n" + "\n".join(view_lines))

        routine_body_lines = [
            f"  {r[0]}: {(r[3] or '').strip()}" for r in routines if (r[3] or "").strip()
        ]
        if routine_body_lines:
            schema_parts.append("Routine definitions:\n" + "\n".join(routine_body_lines))

        with connection.cursor() as cursor:
            # Cardinality gate for the frequent-value sampling below -
            # pg_stats.n_distinct is a planner statistic already computed by
            # Postgres (no live scan) - see _is_near_unique_n_distinct()'s
            # docstring for exactly what this gates and why.
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
                    # A kept_names entry cap_kept_tables dropped columns for
                    # (shouldn't normally happen - kept_names and
                    # column_types are built from the same query - but
                    # guards against an empty/omitted table cleanly).
                    continue

                # Fresh/live row count - authoritative, unlike the Phase 1
                # reltuples estimate above (which is free but can be stale
                # until the next ANALYZE/autovacuum).
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {_quote_ident(table_name)};")
                    row = cursor.fetchone()
                    if row is not None:
                        live_count_lines.append(f"  {table_name}: {row[0]} rows (live, authoritative)")
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

                # Min/max, all eligible numeric/date columns in one combined
                # query per table (bounded by MAX_NUMERIC_COLUMNS_FOR_MINMAX)
                # rather than one query per column.
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
                # per table), skipping any column pg_stats says is close to
                # unique (see _is_near_unique_n_distinct).
                eligible_categorical = []
                for c in categorical_cols:
                    n_distinct = distinct_stats.get(table_name, {}).get(c)
                    if _is_near_unique_n_distinct(n_distinct):
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