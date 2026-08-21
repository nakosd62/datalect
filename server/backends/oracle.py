"""
backends/oracle.py

OracleBackend: talks to Oracle Database via python-oracledb, the official
driver (successor to cx_Oracle). Runs in the driver's default "thin" mode -
a pure-Python/bundled-C-extension implementation that speaks the Oracle
network protocol directly, with no Oracle Instant Client installation
required on the host (verified: it ships prebuilt wheels, like
psycopg2-binary - no Dockerfile changes needed, same as every other
dialect added so far). Thick mode (oracledb.init_oracle_client(), which
DOES need Instant Client) is only required for things this app doesn't
use - OS/Kerberos authentication, Oracle Database 11g support, native
network encryption - so this module never calls it.

Mirrors backends/snowflake.py's/backends/databricks.py's shape more than
backends/postgres.py's: Oracle has no single connection-string form the
driver itself parses, so every connection, preset or custom, needs its own
explicit descriptor rather than a URL.

An Oracle descriptor looks like:
    {"type": "oracle", "host": "...", "port": 1521, "service_name": "...",
     "sid": "...", "user": "...", "password": "...", "schema": "...",
     "ssl": false}
"host"/"user"/"password" are required, and exactly one of "service_name"/
"sid" identifies which (pluggable) database to connect to - "service_name"
is the modern/recommended form; "sid" is kept as a legacy alternate since
older on-prem installs and Oracle XE still commonly use it. "port" defaults
to Oracle's standard listener port (1521) when omitted. "schema" is
optional: Oracle's rough equivalent of a "schema" is actually a *user*
(objects are owned by a user/schema, and they're the same thing) rather
than a separate namespace - omitted, queries run against whichever
schema/owner the connecting user itself is; given, the session switches to
that owner's objects via ALTER SESSION SET CURRENT_SCHEMA right after
connect() (see _set_current_schema below), the same "optional namespace
override" role Snowflake's/Databricks' own "schema" descriptor field
plays.

"ssl" is optional, defaulting to false - plain TCP, matching a typical
on-prem/XE dev instance's listener. Oracle Cloud (including Autonomous
Database) listeners are TLS-only: a plain-TCP connect() attempt against
one doesn't get a DB-API error back, it gets the TCP connection itself
reset ("DPY-4011: the database or network closed the connection") the
moment the driver sends its (non-TLS) initial packet - a confusing failure
mode that looks like a network/firewall problem rather than "wrong
protocol". Setting "ssl": true makes connect() below pass
protocol="tcps"/ssl_server_dn_match=True to oracledb.connect() - verified
against Oracle's own docs ("Connect Python Applications Without a Wallet
(TLS)"): python-oracledb's thin mode can reach ADB over TLS with nothing
more than host/port/service_name/user/password, no wallet file needed, as
long as the target instance's own network settings have "Require mutual
TLS (mTLS) authentication" turned off (an OCI-console-side setting on the
ADB instance itself, outside this app's control) - true wallet-based mTLS
remains the deferred follow-up noted below, not this flag.

This first pass is deliberately narrower than Oracle Database is capable
of, mirroring how Databricks' first pass was PAT-only: only plain
host/port/service_name-or-sid + username/password authentication is
supported. Oracle Autonomous Database's wallet-based mTLS connections ARE
supported by python-oracledb's thin mode too (verified against Oracle's
docs - no Instant Client needed there either), but the driver takes the
wallet as a PEM-format file on disk (wallet_location), not inline text the
way Snowflake's private_key field works - supporting that cleanly would
mean writing pasted wallet content to a temp file per connection, deferred
as a follow-up rather than built into this first pass.

Which of "password" must never round-trip back to the frontend once saved
is state_store.py's _CREDENTIAL_CONFIG_FIELDS' responsibility - "password"
is already covered there (shared with Postgres's URL-embedded password's
sibling field name), no new field name needed.

The connector's declared DB-API paramstyle is "named" (:name placeholders),
same as backends/databricks.py's - get_schema() below reuses that same
_named_in_params pattern for its dynamic IN (...) clauses (duplicated here
rather than imported from backends/databricks.py - each backend module is
self-contained, no cross-dialect imports, same precedent as
app_config.py's own _databricks_url duplication). Unlike Databricks (where
Connection.autocommit is read-only), Oracle's is a normal settable
property, same as backends/postgres.py's - so execute() below just sets it
directly, no read-only-property workaround needed.

Oracle has no ANSI information_schema - schema introspection below uses
Oracle's own data-dictionary views (ALL_TABLES/ALL_TAB_COLUMNS/
ALL_CONSTRAINTS+ALL_CONS_COLUMNS/ALL_VIEWS), scoped by OWNER, resolved via
SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') rather than a hardcoded name (same
principle as Databricks'/Snowflake's current_catalog()/current_schema()
calls) - this correctly reflects the ALTER SESSION SET CURRENT_SCHEMA
switch connect() may have just done, not just the connecting user's own
default schema.

Identifier quoting uses double quotes, and Oracle folds *unquoted*
identifiers to uppercase at parse time - table/column names returned by
the data-dictionary views above will typically already be uppercase unless
they were created with quoted (case-preserving) identifiers. See
translate_routes.py's _DIALECT_PROMPT_INTROS entry for this dialect, which
calls this out so generated SQL doesn't get tripped up assuming
lowercase/mixed-case names resolve unquoted the way Postgres's do.

NOTE for reviewers: like backends/snowflake.py/backends/databricks.py, this
has been exercised against the fake DB-API harness in tests/server/
helpers.py, not a real Oracle Database instance yet - treat the SQL/kwarg
shapes here as a solid first draft, not as already battle-tested the way
backends/postgres.py is. The ALL_TABLES filtering (materialized-view
container tables, IOT overflow/mapping segments, nested-table storage
tables all otherwise polluting a naive "list every table" query) and the
information_schema.tables.table_type gotcha backends/databricks.py hit
after shipping were both verified against Oracle's/Databricks' official
docs specifically because of that earlier lesson - see get_schema() below.
"""

import re

import oracledb
import sqlparse

from .base import (
    Backend, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
)

# CLOB/BLOB columns are fetched as LOB locator objects (requiring .read())
# by default - every other backend's execute() row-shaping below just
# expects plain str/bytes/Decimal/datetime values it can hasattr()-sniff,
# so this disables locator-object fetching globally in favor of plain
# str/bytes, straight from cursor.fetchall() (documented, size-bounded
# behavior - see python-oracledb's "Using CLOB and BLOB Data" guide).
oracledb.defaults.fetch_lobs = False

# Oracle's own identifier grammar (unquoted): a letter, then up to 127
# more letters/digits/underscore/$/# - see _set_current_schema below for
# why this is validated rather than parameterized.
_IDENTIFIER_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_$#]{0,127}$')

_CONSTRAINT_TYPE_LABELS = {"P": "PRIMARY KEY", "U": "UNIQUE", "R": "FOREIGN KEY"}


def _named_in_params(prefix, values):
    """(fragment, params) for a dynamic IN (...) clause under the
    connector's "named" paramstyle (:name, not %s/?) - e.g. for
    values=["a", "b"] and prefix="t", returns (":t0, :t1", {"t0": "a",
    "t1": "b"}). Used wherever get_schema() below needs to scope a query
    to the bounded kept_names set (see backends/base.py)."""
    names = [f"{prefix}{i}" for i in range(len(values))]
    fragment = ", ".join(f":{n}" for n in names)
    return fragment, dict(zip(names, values))


def _set_current_schema(connection, schema):
    """ALTER SESSION SET CURRENT_SCHEMA doesn't accept bind variables -
    Oracle has no parameterized form for session-control statements - so
    `schema` has to be interpolated directly into the SQL text. Validated
    against Oracle's own identifier grammar first (_IDENTIFIER_RE) rather
    than quoted-and-escaped: a value that fails this check isn't a real
    Oracle identifier to begin with, so rejecting it outright is both
    safer and a clearer error than silently quoting arbitrary text.
    Uppercased before use - Oracle folds *unquoted* identifiers to
    uppercase, and virtually every Oracle schema name in the wild is
    all-caps, so a user typing "sales" resolves to the real owner SALES
    the same way it would if they typed it directly into a SQL*Plus
    session, rather than requiring them to know to type it in caps
    themselves."""
    if not _IDENTIFIER_RE.match(schema):
        raise ValueError(
            f"Invalid Oracle schema name: {schema!r} - must be a plain identifier "
            f"(a letter, then letters/digits/underscore/$/# only)."
        )
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {schema.upper()}")


class OracleBackend(Backend):
    dialect_name = "Oracle Database"

    # Oracle has no SELECT-without-FROM form - the base class's plain
    # "SELECT 1" raises ORA-00923: FROM keyword not found where expected.
    # DUAL is Oracle's own single-row/single-column dummy table that exists
    # in every schema for exactly this purpose (see get_schema()'s own
    # SYS_CONTEXT probes elsewhere in this file, which already run against
    # it implicitly via FROM DUAL-shaped queries in identity_label()).
    liveness_sql = "SELECT 1 FROM DUAL"

    def connect(self, descriptor):
        descriptor = descriptor or {}
        host = descriptor.get("host") or ""
        port = descriptor.get("port") or 1521
        service_name = descriptor.get("service_name") or None
        sid = descriptor.get("sid") or None
        user = descriptor.get("user") or ""
        password = descriptor.get("password") or ""
        schema = descriptor.get("schema") or None
        use_ssl = bool(descriptor.get("ssl"))

        if not host:
            raise ValueError("Oracle connection requires a host - none was provided.")
        if not (service_name or sid):
            raise ValueError(
                "Oracle connection requires either a service_name or a sid - neither was provided."
            )
        if not (user and password):
            raise ValueError("Oracle connection requires a user and password - one was missing.")

        # tcp_connect_timeout bounds only the initial TCP connect phase,
        # never query execution afterwards - see backends/base.py's
        # DB_CONNECT_TIMEOUT_SECONDS docstring for why a wrong/unreachable
        # host needs to fail fast here rather than hanging indefinitely.
        kwargs = {
            "host": host, "port": port, "user": user, "password": password,
            "tcp_connect_timeout": float(DB_CONNECT_TIMEOUT_SECONDS),
        }
        if service_name:
            kwargs["service_name"] = service_name
        else:
            kwargs["sid"] = sid
        if use_ssl:
            # ssl_server_dn_match=True is the default python-oracledb itself
            # would use once protocol="tcps" is set, but passed explicitly
            # here rather than relied on - it's the difference between
            # actually validating the server's certificate DN and silently
            # accepting any cert, and that shouldn't depend on the driver's
            # own default staying what it is today. See the module
            # docstring above for why this is opt-in rather than always-on.
            kwargs["protocol"] = "tcps"
            kwargs["ssl_server_dn_match"] = True

        connection = oracledb.connect(**kwargs)

        if schema:
            _set_current_schema(connection, schema)

        return connection

    def close(self, connection):
        # hasattr-guarded like backends/bigquery.py's/backends/snowflake.py's/
        # backends/databricks.py's close() - config_routes.py's /api/config
        # handler calls this unconditionally in a finally block after a
        # best-effort identity_label() probe, including in tests that patch
        # connect() with a lightweight stand-in object that has no close()
        # of its own (see helpers.install_fake_oracle_connect).
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    def cache_key(self, descriptor):
        """host:port/service-or-sid.schema, parsed straight from the
        descriptor - never a credential. Same non-sensitive-identifier role
        SnowflakeBackend's/DatabricksBackend's cache_key plays."""
        descriptor = descriptor or {}
        host = descriptor.get("host") or "unknown"
        port = descriptor.get("port") or "unknown"
        service = descriptor.get("service_name") or descriptor.get("sid") or "unknown"
        schema = descriptor.get("schema") or "unknown"
        return f"{host}:{port}/{service}.{schema}"

    def identity_label(self, connection):
        db_name, username = "Unknown", "Unknown"
        with connection.cursor() as cursor:
            # Reflects whatever connect() actually left CURRENT_SCHEMA as
            # (including a "schema" descriptor override via
            # _set_current_schema above), not just the connecting user's
            # own default schema - SESSION_USER is that connecting user,
            # which can differ from CURRENT_SCHEMA once overridden.
            cursor.execute(
                "SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'), "
                "SYS_CONTEXT('USERENV', 'SESSION_USER') FROM DUAL"
            )
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
            # (see backends/base.py) before any column/constraint/view
            # query runs, same staging as every other backend's
            # get_schema(). Scoped to SYS_CONTEXT('USERENV','CURRENT_SCHEMA')
            # rather than a hardcoded owner name.
            #
            # ALL_TABLES on its own is NOT just "ordinary tables" - it also
            # includes each materialized view's internal storage table
            # (verified against Oracle's docs, after the backends/
            # databricks.py table_type lesson taught this app not to
            # assume): ALL_MVIEWS.MVIEW_NAME anti-joined out below excludes
            # those. IOT_TYPE/NESTED filter out index-organized-table
            # overflow/mapping segments and nested-table storage tables,
            # which would otherwise show up as extra, uninterpretable
            # "tables" alongside real ones.
            cursor.execute("""
                SELECT table_name
                FROM all_tables
                WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                  AND (iot_type IS NULL OR iot_type = 'IOT')
                  AND nested = 'NO'
                  AND table_name NOT IN (
                      SELECT mview_name FROM all_mviews
                      WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                  )
                ORDER BY table_name
                FETCH FIRST :scan_limit ROWS ONLY
            """, {"scan_limit": SCHEMA_MAX_TABLE_NAMES_SCANNED})
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
            # NULLABLE is 'N'/'Y' (not 'NO'/'YES' the way ANSI
            # information_schema.columns.is_nullable is elsewhere).
            in_fragment, in_params = _named_in_params("t", kept_names)
            cursor.execute(f"""
                SELECT table_name, column_name, data_type, nullable
                FROM all_tab_columns
                WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                  AND table_name IN ({in_fragment})
                ORDER BY table_name, column_id
            """, in_params)
            columns_data = cursor.fetchall()

            tables = {}
            for table_name, col_name, data_type, nullable in columns_data:
                tables.setdefault(table_name, []).append(
                    f"  {col_name} {data_type} {'NULL' if nullable == 'Y' else 'NOT NULL'}"
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

            # 2. Constraints - PRIMARY KEY/UNIQUE/FOREIGN KEY only
            # (constraint_type also covers CHECK ('C') and view-related
            # codes, not useful context for SQL generation here).
            # constraint_type is a single-letter code (P/U/R), mapped to a
            # readable label via _CONSTRAINT_TYPE_LABELS for consistency
            # with every other backend's spelled-out constraint type text.
            # Best-effort: a role without dictionary-view access on
            # ALL_CONSTRAINTS/ALL_CONS_COLUMNS degrades to "skip this
            # section", not a failed schema fetch (mirrors backends/
            # bigquery.py's/backends/snowflake.py's/backends/databricks.py's
            # same try/except).
            try:
                cursor.execute(f"""
                    SELECT ac.table_name, ac.constraint_name, ac.constraint_type, acc.column_name
                    FROM all_constraints ac
                    JOIN all_cons_columns acc
                      ON ac.owner = acc.owner
                     AND ac.constraint_name = acc.constraint_name
                     AND ac.table_name = acc.table_name
                    WHERE ac.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                      AND ac.table_name IN ({in_fragment})
                      AND ac.constraint_type IN ('P', 'U', 'R')
                    ORDER BY ac.table_name, ac.constraint_name, acc.position
                """, in_params)
                constraint_rows = cursor.fetchall()
                if constraint_rows:
                    lines = [
                        f"  [{t}] {n} ({_CONSTRAINT_TYPE_LABELS.get(ty, ty)}): {c}"
                        for (t, n, ty, c) in constraint_rows
                    ]
                    schema_parts.append("Constraints:\n" + "\n".join(lines))
            except Exception:
                pass

            # 3. Views - deliberately NOT scoped to kept_names, same
            # reasoning as every other backend here: that set is built
            # exclusively from ALL_TABLES rows, so no view name could ever
            # appear in it. TEXT_VC (VARCHAR2(4000)) is used instead of
            # TEXT (a LONG column, with the usual LONG-fetching
            # restrictions) - this may truncate a very long view
            # definition, an accepted tradeoff for schema-summary context
            # rather than a full DDL dump. TEXT_VC doesn't exist on every
            # Oracle version this app might connect to, so this is
            # best-effort like the constraints section above - a version
            # without it just skips this section.
            try:
                cursor.execute("""
                    SELECT view_name, text_vc
                    FROM all_views
                    WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
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

            # Deliberately no Indexes/Triggers/Grants sections: left for
            # follow-up, same status backends/snowflake.py's/backends/
            # databricks.py's own gaps have - not verified against a real
            # Oracle instance as part of this first pass.

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
