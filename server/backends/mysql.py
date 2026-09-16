"""
backends/mysql.py

MySQLBackend: talks to any MySQL-compatible database via PyMySQL (a pure-
Python DB-API driver - no compiled extension, so unlike a C-extension
driver such as mysqlclient, this needed no Dockerfile changes to add,
mirroring how psycopg2-binary already avoids needing libpq-dev). Mirrors
backends/postgres.py's shape closely: like Postgres, MySQL has a single
connection-string form and no BigQuery-style ambient identity or
Snowflake-style always-explicit multi-field credential to worry about, so
this is the "simple URL dialect" pattern, not the structured-descriptor
one bigquery.py/snowflake.py use.

A MySQL descriptor looks like: {"type": "mysql", "url": "mysql://user:
password@host:port/dbname"}. Unlike psycopg2.connect(), which accepts a
full DSN string directly, PyMySQL's connect() takes individual keyword
arguments - so connect() below parses the URL itself (via urlparse) and
percent-decodes the username/password, since urlparse does NOT do that
implicitly (unlike psycopg2's own DSN parser).

Cloud SQL (and other environments reached over a Unix domain socket
rather than TCP - e.g. Cloud Run talking to a Cloud SQL instance via its
/cloudsql/<CONNECTION_NAME> socket mount) needs a different connect()
shape entirely: no real host, and a `unix_socket` path instead. This is
expressed the same way GCP's own docs/SQLAlchemy examples do it for a
PyMySQL-based connection string - a `unix_socket` query parameter, with
the host left blank, e.g.:
    mysql://user:pass@/dbname?unix_socket=/cloudsql/project:region:instance
_parse_mysql_url() below looks for that query parameter and, when
present, tells connect() to use it instead of host/port - a plain
"host or 'localhost'" fallback (this module's original, incomplete
version) would otherwise silently try - and fail - a TCP connection to
localhost, since a socket-only URL has no real host in it at all.

Schema introspection differs from Postgres in a few real ways worth
calling out, since it's easy to port the Postgres queries wrong by
assuming they're identical:
  - MySQL has no separate "schema" concept the way Postgres has "public" -
    a schema *is* a database. Every information_schema query below scopes
    to TABLE_SCHEMA = DATABASE() (the database named in the connection
    URL's path), not a hardcoded schema name.
  - Identifier quoting uses backticks, not double quotes (see
    translate_routes.py's _DIALECT_PROMPT_INTROS entry for this dialect).
  - Indexes come from information_schema.STATISTICS, not pg_indexes.
  - Foreign keys/constraints come from TABLE_CONSTRAINTS joined with
    KEY_COLUMN_USAGE (REFERENCED_TABLE_NAME/REFERENCED_COLUMN_NAME already
    live on KEY_COLUMN_USAGE itself in MySQL - no separate
    constraint_column_usage-style join needed, unlike Postgres).
  - There is no role_table_grants: MySQL's equivalent is
    information_schema.TABLE_PRIVILEGES (per-table grants; there's also
    SCHEMA_PRIVILEGES for database-level grants, but table-level is the
    closer match to what the Postgres backend surfaces).

NOTE for reviewers: like backends/snowflake.py, this has been exercised
against the fake DB-API harness in tests/server/helpers.py, not a real
MySQL server yet - treat the SQL here as a solid first draft.

TLS ("sslmode", a CA certificate to verify against) works differently here
than in backends/postgres.py, because of that same "connect() parses the
URL itself" fact above: psycopg2.connect() forwards a whole DSN string to
libpq, which parses "?sslmode=..."/"?sslrootcert=..." itself - but this
module already hand-parses the URL's query string and only recognizes
specific keys (see _parse_mysql_url), so "sslmode" needs to be explicitly
added to that whitelist rather than "just working" the way it does for
Postgres. See _parse_mysql_url's and connect()'s own comments for the
recognized "sslmode" values (deliberately reusing Postgres's own
disable/require/verify-ca/verify-full vocabulary, even though MySQL's own
client tools spell these differently, so a user moving between the two
dialects in this app doesn't need to learn two vocabularies for the same
idea) and how each maps onto PyMySQL's ssl_* parameters.

Worth correcting here for anyone who read this module's connect() before
this comment existed: PyMySQL does NOT leave a connection fully
unencrypted by default the way this docstring used to imply - with no
ssl_* arguments at all (sslmode absent/"disable", the byte-identical-to-
before case), PyMySQL still opportunistically attempts TLS if the server
offers it and silently falls back to plaintext if not (its own comment
calls this "PREFERRED mode") - the same "prefer" default psycopg2/libpq
uses for Postgres when nothing is specified. "require"/"verify-ca"/
"verify-full" below are about turning that opportunistic, unverified
default into something enforced and/or actually checked - not about
turning encryption on from a fully-off state.
"""

import os
import ssl
from urllib.parse import urlparse, unquote, parse_qs

import pymysql
import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS, resolve_timeout_seconds, materialize_ca_cert_tempfile,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text, fetch_capped_rows,
    find_naming_convention_relationships,
)


def _quote_ident(name):
    """Backtick-quotes a MySQL identifier for interpolation into a plain SQL
    string (doubling an embedded backtick, the way MySQL itself expects),
    for the Phase 2 per-table live-query section below (get_schema()'s
    "Column value samples"/"Live row counts" sections) - mirrors
    backends/postgres.py's own _quote_ident helper, just with MySQL's
    backtick quoting instead of Postgres's double quotes. `name` always
    comes from information_schema data this same connection already
    queried (kept_names / column names Phase 1 just fetched) - never from
    raw user input - so this only needs to be correct, not defend against
    an adversarial identifier."""
    return '`' + str(name).replace('`', '``') + '`'


# Phase 2 (deep-only) sampling: which information_schema.columns.DATA_TYPE
# strings (MySQL reports these lowercased, e.g. "int", "varchar") are worth a
# MIN()/MAX() range query (numeric/date-ish) vs a frequent-value GROUP BY
# (bounded/categorical-ish) - mirrors backends/postgres.py's
# NUMERIC_OR_DATE_TYPES/CATEGORICAL_TYPES, adapted to MySQL's own type names.
# Deliberately conservative/small lists rather than "everything that isn't
# the other list" - a DATA_TYPE this doesn't recognize (json, blob, binary,
# geometry, ...) is simply skipped for sampling rather than guessed at.
NUMERIC_OR_DATE_TYPES = frozenset({
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
    "decimal", "numeric", "float", "double",
    "date", "datetime", "timestamp", "time", "year",
})
CATEGORICAL_TYPES = frozenset({"varchar", "char", "tinytext", "enum", "set"})

# Bounds on Phase 2's per-table sampling cost - see get_schema()'s "Column
# value samples" section for how each is used. Same values as
# backends/postgres.py's own constants, kept as separate module-level
# constants (not imported) since each backend's Phase 2 section is
# independently tunable.
MAX_COLUMNS_FOR_SAMPLING = 25
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
# MySQL has no free planner statistic as cheap as Postgres's pg_stats.n_distinct
# to gate frequent-value sampling with (see _is_near_unique_distinct_count's
# docstring below for why a live COUNT(DISTINCT ...) is used instead) - that
# means checking a categorical column's cardinality here always costs one
# extra live query, unlike Postgres where the gate is free. This second cap
# bounds how many categorical columns even get that COUNT(DISTINCT ...)
# check per table, independent of how many turn out eligible - without it, a
# table with many categorical columns that all happen to be near-unique
# would still cost one live query per column just to rule each one out.
MAX_CATEGORICAL_COLUMNS_CHECKED_PER_TABLE = 10
# Matches the "GROUP BY ... ORDER BY COUNT(*) DESC LIMIT 15" shape called
# for in the plan this implements.
FREQUENT_VALUES_LIMIT = 15


def _is_near_unique_distinct_count(distinct_count, row_count):
    """Gate for whether a categorical column's "frequent values" sample is
    worth rendering at all - using a live `SELECT COUNT(DISTINCT col)`
    rather than a pre-computed planner statistic the way
    backends/postgres.py's _is_near_unique_n_distinct does with
    pg_stats.n_distinct. MySQL's closest catalog analog,
    information_schema.STATISTICS.CARDINALITY, only exists for indexed
    columns and is itself just an estimate refreshed on ANALYZE TABLE/some
    engine operations - not reliable enough to gate on for an arbitrary
    categorical column that may have no index at all. A live COUNT(DISTINCT)
    is used instead: this is acceptable here specifically because gating
    only ever runs from get_schema() (deep, Phase 2, already a live-query
    context) - get_schema_shallow() (Phase 1) never reaches this function.

    `distinct_count is None` (the COUNT(DISTINCT) query itself failed, e.g.
    a permissions gap) is treated as "not near-unique" - permissive by
    design, matching backends/postgres.py's own None handling, since the
    alternative (always skipping a column this couldn't check) would hide
    sampling entirely rather than just occasionally rendering a sample for
    an unexpectedly-unique column. A column is near-unique when it has more
    than 1000 distinct values outright (mirrors Postgres's own absolute
    cutoff), or - for a smaller table where 1000 alone wouldn't catch a
    fully-unique column - when at least half of all rows have a distinct
    value (`row_count` is the live row count this same Phase 2 pass already
    fetched for the table, so this needs no extra query of its own)."""
    if distinct_count is None:
        return False
    if distinct_count > 1000:
        return True
    if row_count:
        return distinct_count / row_count >= 0.5
    return False


def _parse_mysql_url(url):
    """{"host", "port", "user", "password", "database", "unix_socket",
    "sslmode"} from a mysql://... URL. urlparse does not percent-decode
    .username/.password (unlike psycopg2's own DSN parser), so both are
    explicitly unquote()'d here - without this, a password containing an
    encoded special character (e.g. %40 for '@') would be sent to the
    server still percent-encoded and fail to authenticate.

    "unix_socket" is pulled from the query string (see module docstring -
    the Cloud SQL connection pattern) and is None when absent, i.e. an
    ordinary TCP connection. "sslmode" is the other recognized key (see
    connect() for what each value does) - a query string can carry other
    keys too (charset, ...); anything besides these two is silently
    ignored rather than erroring, same as an unrecognized field would be
    for any other dialect's config."""
    parsed = urlparse(url)
    database = parsed.path.lstrip('/')
    if '?' in database:
        database = database.split('?')[0]
    query = parse_qs(parsed.query)
    unix_socket = (query.get('unix_socket') or [None])[0]
    sslmode = (query.get('sslmode') or [None])[0]
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username) if parsed.username else "",
        "password": unquote(parsed.password) if parsed.password else "",
        "database": database or None,
        "unix_socket": unix_socket,
        "sslmode": sslmode,
    }


class MySQLBackend(Backend):
    dialect_name = "MySQL"

    def connect(self, descriptor):
        descriptor = descriptor or {}
        parts = _parse_mysql_url(descriptor.get("url") or "")
        kwargs = {
            "user": parts["user"], "password": parts["password"], "database": parts["database"],
            "autocommit": False,
            # connect_timeout bounds only TCP/handshake setup, never query
            # execution afterwards - see backends/base.py's
            # DB_CONNECT_TIMEOUT_SECONDS docstring. PyMySQL already defaults
            # this to 10 on its own, but set it explicitly here so it's tied
            # to the same single, admin-adjustable knob every other dialect
            # uses rather than to a value that happens to coincide with it.
            # resolve_timeout_seconds() lets this preset/custom connection's
            # own "connect_timeout_seconds" override that shared default.
            "connect_timeout": resolve_timeout_seconds(
                descriptor, "connect_timeout_seconds", DB_CONNECT_TIMEOUT_SECONDS,
            ),
        }
        if parts["unix_socket"]:
            # Unix-socket connections (Cloud SQL) have no real TCP host at
            # all - host/port are omitted entirely rather than sent
            # alongside unix_socket, so there's no ambiguity about which
            # one PyMySQL actually uses. TLS is a TCP-only concept in the
            # MySQL wire protocol too (same as Postgres - see
            # backends/postgres.py's docstring for the Postgres side of
            # this), so sslmode is meaningless for a unix_socket connection
            # and is intentionally never even inspected in this branch.
            kwargs["unix_socket"] = parts["unix_socket"]
            return pymysql.connect(**kwargs)

        kwargs["host"] = parts["host"]
        kwargs["port"] = parts["port"]

        # sslmode is never forced or defaulted here, same policy as
        # backends/postgres.py's sslmode: "disable" (or the key simply
        # absent from the URL, the overwhelmingly common case and the one
        # that must behave byte-identically to before this feature
        # existed) means "don't add anything - let PyMySQL's own built-in
        # opportunistic-TLS-with-silent-fallback default apply" (see module
        # docstring for why that's already the behavior with zero ssl_*
        # kwargs at all, not "always plaintext").
        #
        # "require"/"verify-ca"/"verify-full" all build our own
        # ssl.SSLContext by hand and hand it to PyMySQL via the "ssl"
        # kwarg, rather than using PyMySQL's own ssl_ca/ssl_verify_cert/
        # ssl_verify_identity kwargs directly - PyMySQL special-cases an
        # ssl.SSLContext instance as "use this exactly as given" (see
        # Connection._create_ssl_ctx), which sidesteps PyMySQL's own
        # somewhat surprising interaction between those individual kwargs
        # (e.g. supplying ssl_ca alone, with ssl_verify_cert/
        # ssl_verify_identity left unset, does NOT turn on verification at
        # all - it silently maps to CERT_NONE) in favor of us stating
        # exactly what we mean for each mode.
        sslmode = (parts.get("sslmode") or "").strip().lower()
        ca_cert_pem = descriptor.get("ca_cert_pem")
        temp_ca_path = None
        try:
            if sslmode == "require":
                # Encrypt, but don't check the server's certificate at all
                # - same meaning as Postgres's sslmode=require. No CA cert
                # is used or needed for this mode.
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                kwargs["ssl"] = ctx
            elif sslmode in ("verify-ca", "verify-full"):
                # cafile passed straight to create_default_context() (not
                # added afterward via load_verify_locations()) so that,
                # when a CA cert IS supplied, ONLY that CA is trusted -
                # not also this machine's system CA store - mirroring
                # libpq's own sslrootcert semantics (see
                # backends/postgres.py's connect()). Without a CA cert,
                # this falls back to the system trust store, same as
                # Postgres does when verify-full is requested with no
                # sslrootcert at all.
                if ca_cert_pem:
                    temp_ca_path = materialize_ca_cert_tempfile(ca_cert_pem)
                    ctx = ssl.create_default_context(cafile=temp_ca_path)
                else:
                    ctx = ssl.create_default_context()
                ctx.check_hostname = (sslmode == "verify-full")
                ctx.verify_mode = ssl.CERT_REQUIRED
                kwargs["ssl"] = ctx
            # Any other value (missing, "disable", or an unrecognized typo)
            # intentionally adds nothing at all - see the comment above.

            return pymysql.connect(**kwargs)
        finally:
            # Only needed for the handshake inside pymysql.connect() above
            # - same reasoning as backends/postgres.py's own tempfile
            # cleanup: safe (and best practice, since this is derived from
            # user-pasted PEM text) to remove it immediately rather than
            # leaving it on disk any longer than this one connect() call
            # needs it.
            if temp_ca_path:
                try:
                    os.remove(temp_ca_path)
                except OSError:
                    pass

    def close(self, connection):
        # hasattr-guarded like backends/bigquery.py's and
        # backends/snowflake.py's close() (not just `if connection:` the
        # way backends/postgres.py's does) - config_routes.py's /api/config
        # handler calls this unconditionally in a finally block after a
        # best-effort identity_label() probe, including in tests that patch
        # connect() with a lightweight stand-in object that has no close()
        # of its own (see helpers.install_fake_pymysql_connect).
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    def cache_key(self, descriptor):
        """username@host:port/dbname (or username@unix_socket/dbname for a
        Cloud SQL-style socket connection), parsed from the connection URL -
        never the URL itself, since that carries the password. Mirrors
        backends/postgres.py's cache_key() derivation and its reasoning:
        host/port (or unix_socket) matters just as much as dbname does -
        two entirely different MySQL servers can easily share both a
        username and a database name, and without the host in the key both
        would collide on the same schema_cache.py entry, silently serving
        one server's schema back for the other's /api/translate calls.
        Username is still included too: two different users against the
        exact same database can see different information_schema results
        if their grants differ.

        unix_socket (not host:port) is used as the target component when
        present - _parse_mysql_url() defaults "host" to the meaningless
        "localhost" for a socket connection (see its own docstring), so
        using host:port here would collide two different Cloud SQL
        instances (different unix_socket paths) onto the same
        "user@localhost:3306/db" key just as easily as the original,
        completely host-blind version of this method did."""
        url = (descriptor or {}).get("url")
        if not url:
            return "unknown@unknown"
        try:
            parts = _parse_mysql_url(url)
            target = parts.get("unix_socket") or f"{parts['host']}:{parts['port']}"
            return f"{parts['user'] or 'unknown'}@{target}/{parts['database'] or 'unknown'}"
        except Exception:
            return "unknown@unknown"

    def identity_label(self, connection):
        db_name, username = "Unknown", "Unknown"
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE(), CURRENT_USER();")
            row = cursor.fetchone()
            if row:
                db_name, username = row[0], row[1]
        return db_name, username

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - see backends/postgres.py's own
        _build_shallow_schema_parts() for the identical shape this mirrors,
        and backends/base.py's Backend.get_schema()/get_schema_shallow()
        docstrings for why this split exists at all.

        Returns None if the connection's database has no BASE TABLE at all
        (mirrors get_schema()'s old "return None" for that case). Otherwise
        returns (schema_parts, table_columns, phase2_ctx):
          - schema_parts: the ordered list of text sections, not yet joined/
            capped.
          - table_columns: {table_name: [column_name, ...]}, scoped to the
            same bounded kept_names set schema_parts describes - handed to
            the shared find_naming_convention_relationships() helper by
            get_schema()'s Phase 2 pass (no extra query needed for that).
          - phase2_ctx: raw, already-fetched data Phase 2 wants to reuse
            without re-querying - kept_names/column_types (for deciding what
            to sample) and the raw views/routines rows (so get_schema() can
            render their full body text without a second trip to the
            database - see the "Views"/"Routines" sections below for why
            only the *name*/signature is rendered here).
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
            # index/view/trigger query runs, same staging as
            # backends/postgres.py's own version.
            cursor.execute("""
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
                LIMIT %s;
            """, (SCHEMA_MAX_TABLE_NAMES_SCANNED,))
            all_table_names = [row[0] for row in cursor.fetchall()]

            if not all_table_names:
                return None

            kept_names, shard_groups = group_date_sharded_tables(all_table_names)
            kept_names, shard_groups, omitted_count = cap_kept_tables(kept_names, shard_groups)
            shard_by_representative = {
                members[-1]: (prefix, members) for prefix, members in shard_groups.items()
            }

            # 1. Tables and Columns - scoped to the bounded kept_names set.
            # EXTRA/COLUMN_COMMENT (new) are appended at the end of the
            # SELECT list (not inserted in the middle) so that a test/caller
            # still passing the old 5-column row shape can be padded rather
            # than needing every existing row rewritten - see
            # tests/server/test_mysql_backend.py's own _pad_column_row.
            # EXTRA contains the literal substring "auto_increment" for an
            # AUTO_INCREMENT column (MySQL's identity-column equivalent -
            # see the plan's "Identity/auto-increment marker" attribute);
            # COLUMN_COMMENT is whatever comment text was set on the column
            # via COMMENT '...' at CREATE/ALTER TABLE time (empty string,
            # never NULL, when none was set).
            format_strings = ",".join(["%s"] * len(kept_names))
            cursor.execute(f"""
                SELECT
                    c.TABLE_NAME,
                    c.COLUMN_NAME,
                    c.DATA_TYPE,
                    c.IS_NULLABLE,
                    c.COLUMN_DEFAULT,
                    c.EXTRA,
                    c.COLUMN_COMMENT
                FROM information_schema.COLUMNS c
                WHERE c.TABLE_SCHEMA = DATABASE()
                  AND c.TABLE_NAME IN ({format_strings})
                ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION;
            """, tuple(kept_names))
            columns_data = cursor.fetchall()

            tables = {}
            column_comments = {}
            for row in columns_data:
                table_name, col_name, data_type, is_nullable, col_default, extra, col_comment = row
                tables.setdefault(table_name, [])
                table_columns.setdefault(table_name, []).append(col_name)
                column_types.setdefault(table_name, {})[col_name] = data_type
                default_str = f" DEFAULT {col_default}" if col_default is not None else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                extra_str = ""
                if extra and "auto_increment" in extra.lower():
                    extra_str = " AUTO_INCREMENT"
                tables[table_name].append(
                    f"  {col_name} {data_type} {null_str}{default_str}{extra_str}"
                )
                if col_comment:
                    column_comments.setdefault(table_name, []).append((col_name, col_comment))

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

            # 2. Constraints - MySQL's KEY_COLUMN_USAGE already carries
            # REFERENCED_TABLE_NAME/REFERENCED_COLUMN_NAME directly (unlike
            # Postgres, which needs a separate constraint_column_usage
            # join - see backends/postgres.py), so one join is enough here.
            cursor.execute(f"""
                SELECT
                    tc.TABLE_NAME,
                    tc.CONSTRAINT_NAME,
                    tc.CONSTRAINT_TYPE,
                    kcu.COLUMN_NAME,
                    kcu.REFERENCED_TABLE_NAME,
                    kcu.REFERENCED_COLUMN_NAME
                FROM information_schema.TABLE_CONSTRAINTS tc
                LEFT JOIN information_schema.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                 AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                 AND tc.TABLE_NAME = kcu.TABLE_NAME
                WHERE tc.TABLE_SCHEMA = DATABASE()
                  AND tc.TABLE_NAME IN ({format_strings})
                ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME;
            """, tuple(kept_names))
            constraints = cursor.fetchall()
            if constraints:
                constraint_lines = []
                for tbl, c_name, c_type, col, f_tbl, f_col in constraints:
                    if c_type == 'FOREIGN KEY' and f_tbl:
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type}): {col} -> {f_tbl}({f_col})")
                    elif col:
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type}): {col}")
                    else:
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type})")
                schema_parts.append("Constraints:\n" + "\n".join(constraint_lines))

            # 3. Indexes
            cursor.execute(f"""
                SELECT
                    TABLE_NAME,
                    INDEX_NAME,
                    COLUMN_NAME,
                    NON_UNIQUE,
                    SEQ_IN_INDEX
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN ({format_strings})
                ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
            """, tuple(kept_names))
            index_rows = cursor.fetchall()
            if index_rows:
                indexes = {}
                for tbl, idx_name, col, non_unique, _seq in index_rows:
                    indexes.setdefault((tbl, idx_name), {"cols": [], "unique": not non_unique})
                    indexes[(tbl, idx_name)]["cols"].append(col)
                idx_lines = []
                for (tbl, idx_name), info in indexes.items():
                    kind = "UNIQUE" if info["unique"] else "INDEX"
                    idx_lines.append(f"  [{tbl}] {idx_name} ({kind}): {', '.join(info['cols'])}")
                schema_parts.append("Indexes:\n" + "\n".join(idx_lines))

            # 4. Views - deliberately NOT scoped to kept_names, same
            # reasoning as backends/postgres.py's get_schema(): kept_names
            # is built exclusively from BASE TABLE names, so no view name
            # could ever appear in it.
            #
            # Shallow rendering is name-only (no VIEW_DEFINITION body) - the
            # raw rows (including each view's body) are still fetched here
            # (one query, reused by both phases) and threaded through via
            # phase2_ctx below so get_schema() (deep) can render the full
            # body without a second query - see get_schema()'s "View
            # definitions" section.
            cursor.execute("""
                SELECT TABLE_NAME, VIEW_DEFINITION
                FROM information_schema.VIEWS
                WHERE TABLE_SCHEMA = DATABASE();
            """)
            views = cursor.fetchall()
            if views:
                view_lines = [f"  View {v[0]}" for v in views]
                schema_parts.append("Views:\n" + "\n".join(view_lines))

            # 5. Grants - MySQL's closest equivalent to Postgres's
            # role_table_grants is information_schema.TABLE_PRIVILEGES
            # (per-table grants; SCHEMA_PRIVILEGES also exists for
            # database-level grants, but table-level is the closer match
            # to what the Postgres backend surfaces here).
            cursor.execute(f"""
                SELECT GRANTEE, TABLE_NAME, PRIVILEGE_TYPE
                FROM information_schema.TABLE_PRIVILEGES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN ({format_strings})
                ORDER BY TABLE_NAME, GRANTEE;
            """, tuple(kept_names))
            grants = cursor.fetchall()
            if grants:
                grant_lines = [f"  Grant {g[2]} on {g[1]} to {g[0]}" for g in grants]
                schema_parts.append("Grants:\n" + "\n".join(grant_lines))

            # 6. Triggers
            cursor.execute(f"""
                SELECT EVENT_OBJECT_TABLE, TRIGGER_NAME, EVENT_MANIPULATION, ACTION_STATEMENT
                FROM information_schema.TRIGGERS
                WHERE EVENT_OBJECT_SCHEMA = DATABASE()
                  AND EVENT_OBJECT_TABLE IN ({format_strings});
            """, tuple(kept_names))
            triggers = cursor.fetchall()
            if triggers:
                trig_lines = [f"  [{t[0]}] {t[1]} ({t[2]}): {t[3]}" for t in triggers]
                schema_parts.append("Triggers:\n" + "\n".join(trig_lines))

            # 7. Table comments + row count estimate (new) - both live on
            # the exact same information_schema.TABLES row per kept table,
            # so one query covers two new attribute groups (table/column
            # comments, and the row-count estimate) instead of two separate
            # round trips to the same catalog view. Column comments were
            # already collected above from the COLUMNS query itself
            # (column_comments); combined with this query's table-level
            # comments into a single "Comments:" section below.
            #
            # TABLE_ROWS is an InnoDB *estimate* (refreshed by ANALYZE
            # TABLE/some background operations, not a live scan) - it can
            # read 0 or be stale for a table that hasn't been analyzed
            # recently, especially right after a server restart (InnoDB's
            # own documented caveat); see get_schema()'s "Live row counts"
            # section for the authoritative, deep-only counterpart. Best-
            # effort/try-except: a role that can't evaluate this still gets
            # every other section, rather than losing the whole schema
            # fetch over one cosmetic/estimate addition.
            try:
                cursor.execute(f"""
                    SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME IN ({format_strings});
                """, tuple(kept_names))
                table_meta = cursor.fetchall()
            except Exception:
                table_meta = []

            table_comments = {}
            estimate_lines = []
            for tbl, table_comment, table_rows in table_meta:
                if table_comment:
                    table_comments[tbl] = table_comment
                if table_rows is not None:
                    estimate_lines.append(
                        f"  {tbl}: ~{int(table_rows)} rows (estimate - InnoDB "
                        f"statistics, may be 0 or stale for some tables)"
                    )

            comment_lines = []
            for table_name in kept_names:
                if table_name in table_comments:
                    comment_lines.append(f"  [table] {table_name}: {table_comments[table_name]}")
                for col_name, col_comment in column_comments.get(table_name, []):
                    comment_lines.append(f"  [column] {table_name}.{col_name}: {col_comment}")
            if comment_lines:
                schema_parts.append("Comments:\n" + "\n".join(comment_lines))
            if estimate_lines:
                schema_parts.append("Row count estimates:\n" + "\n".join(estimate_lines))

            # 8. Routines (new) - existence + signature only, no body (see
            # get_schema()'s "Routine definitions" section for the full-body
            # deep-only counterpart, reusing ROUTINE_DEFINITION fetched here
            # rather than re-querying it). Not scoped to kept_names (like
            # Views above) - routines aren't tables and ROUTINE_SCHEMA =
            # DATABASE() already bounds this to the connection's own schema.
            #
            # PARAMETER_MODE IS NOT NULL on the join excludes a FUNCTION's
            # own return-value row from information_schema.PARAMETERS (MySQL
            # represents a function's return type as a parameter-shaped row
            # with PARAMETER_MODE/PARAMETER_NAME both NULL) - without this,
            # a function's signature would include a spurious "None None"
            # entry alongside its real parameters.
            routines = []
            try:
                cursor.execute("""
                    SELECT r.ROUTINE_NAME,
                           COALESCE(GROUP_CONCAT(
                               CONCAT(p.PARAMETER_NAME, ' ', p.DATA_TYPE)
                               ORDER BY p.ORDINAL_POSITION SEPARATOR ', '
                           ), '') AS signature,
                           r.DATA_TYPE AS return_type,
                           r.ROUTINE_DEFINITION
                    FROM information_schema.ROUTINES r
                    LEFT JOIN information_schema.PARAMETERS p
                      ON p.SPECIFIC_SCHEMA = r.ROUTINE_SCHEMA
                     AND p.SPECIFIC_NAME = r.SPECIFIC_NAME
                     AND p.PARAMETER_MODE IS NOT NULL
                    WHERE r.ROUTINE_SCHEMA = DATABASE()
                    GROUP BY r.ROUTINE_NAME, r.SPECIFIC_NAME, r.DATA_TYPE, r.ROUTINE_DEFINITION
                    ORDER BY r.ROUTINE_NAME;
                """)
                routines = cursor.fetchall()
                if routines:
                    routine_lines = []
                    for name, signature, return_type, _definition in routines:
                        if return_type:
                            routine_lines.append(f"  {name}({signature}) -> {return_type}")
                        else:
                            routine_lines.append(f"  {name}({signature})")
                    schema_parts.append("Routines:\n" + "\n".join(routine_lines))
            except Exception:
                pass

            # 9. Session timezone / default collation (new) - one line for
            # the whole connection, not per-table. @@session.time_zone is
            # the session's effective timezone (what TIMESTAMP arithmetic
            # and NOW()/CURRENT_TIMESTAMP resolve against);
            # @@collation_database is the connected database's default
            # collation (affects text ordering/comparison).
            try:
                cursor.execute("SELECT @@session.time_zone, @@collation_database;")
                row = cursor.fetchone()
                if row:
                    tz, collation = row[0], row[1]
                    schema_parts.append(f"Session: timezone={tz}; default collation={collation}")
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
        """Deep fetch: Phase 1 (catalog-only, via _build_shallow_schema_parts)
        plus Phase 2 (live-query enrichment) - see get_schema_shallow() for
        the catalog-only subset, and the module-level constants above for
        the sampling caps this section respects."""
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
            schema_parts.append("View definitions:\n" + "\n".join(view_lines))

        routine_body_lines = [
            f"  {r[0]}: {(r[3] or '').strip()}" for r in routines if (r[3] or "").strip()
        ]
        if routine_body_lines:
            schema_parts.append("Routine definitions:\n" + "\n".join(routine_body_lines))

        with connection.cursor() as cursor:
            live_count_lines = []
            sample_blocks = []
            row_counts = {}

            for table_name in kept_names:
                col_types = column_types.get(table_name) or {}
                if not col_types:
                    # A kept_names entry cap_kept_tables dropped columns for
                    # (shouldn't normally happen - kept_names and
                    # column_types are built from the same query - but
                    # guards against an empty/omitted table cleanly).
                    continue

                # Fresh/live row count - authoritative, unlike the Phase 1
                # TABLE_ROWS estimate above (which is free but can be 0/
                # stale until the next ANALYZE TABLE/background refresh).
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {_quote_ident(table_name)};")
                    row = cursor.fetchone()
                    if row is not None:
                        row_counts[table_name] = row[0]
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
                categorical_cols = [
                    c for c, t in col_types.items() if t in CATEGORICAL_TYPES
                ][:MAX_CATEGORICAL_COLUMNS_CHECKED_PER_TABLE]

                table_sample_lines = []

                # Min/max, all eligible numeric/date columns in one combined
                # query per table (bounded by MAX_NUMERIC_COLUMNS_FOR_MINMAX)
                # rather than one query per column.
                if numeric_cols:
                    try:
                        select_parts = ", ".join(
                            f"MIN({_quote_ident(c)}) AS `{c}__min`, MAX({_quote_ident(c)}) AS `{c}__max`"
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

                # Frequent values - one COUNT(DISTINCT ...) gate query plus
                # (if eligible) one frequent-value GROUP BY query per
                # categorical column, capped at
                # MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE eligible columns
                # per table (categorical_cols itself is already capped at
                # MAX_CATEGORICAL_COLUMNS_CHECKED_PER_TABLE candidates - see
                # that constant's own docstring for why a second cap is
                # needed here, unlike Postgres's free pg_stats gate).
                eligible_count = 0
                for c in categorical_cols:
                    if eligible_count >= MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE:
                        break
                    try:
                        cursor.execute(
                            f"SELECT COUNT(DISTINCT {_quote_ident(c)}) FROM {_quote_ident(table_name)};"
                        )
                        row = cursor.fetchone()
                        distinct_count = row[0] if row is not None else None
                    except Exception:
                        distinct_count = None

                    if _is_near_unique_distinct_count(distinct_count, row_counts.get(table_name)):
                        continue
                    eligible_count += 1

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
        connection.autocommit(True)

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
