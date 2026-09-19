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
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS, resolve_timeout_seconds,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text, fetch_capped_rows,
    find_naming_convention_relationships, min_frequent_value_count, FREQUENT_VALUES_LIMIT,
    format_dataset_size_line, format_multiline_schema_entry_body,
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

# A line that (after stripping surrounding whitespace) is exactly a single
# "/" - the SQL*Plus/SQLcl convention for terminating a PL/SQL anonymous
# block or a CREATE ... PROCEDURE/FUNCTION/PACKAGE/TRIGGER/TYPE body. This
# is a CLIENT-side script-parsing directive, never part of the SQL/PL-SQL
# language itself - python-oracledb's cursor.execute() must never receive
# it as SQL text (verified against python-oracledb's own "Executing
# PL/SQL" docs: its anonymous-block example passes the block text ending
# in "end;" straight to execute(), no trailing "/" involved at all).
_SLASH_TERMINATOR_RE = re.compile(r'(?m)^[ \t]*/[ \t]*$')

# A chunk that starts with one of these (after stripping leading
# whitespace) is a PL/SQL unit that must be sent to Oracle as ONE single,
# complete statement - unlike ordinary SQL, its internal semicolons (each
# variable declaration in DECLARE, each statement inside BEGIN...END) are
# part of the unit's own grammar, not statement separators. sqlparse.split()
# has no notion of PL/SQL block structure (it only special-cases things
# like $$-dollar-quoting, which Oracle PL/SQL doesn't use at all) - fed a
# whole DECLARE/BEGIN/END block, it splits at every semicolon it sees,
# including the one ending "DECLARE v_count NUMBER;", truncating the block
# before Oracle ever receives a complete unit. This is exactly the
# PLS-00103 "'end-of-file' ... not null range default character" error
# this regex-and-chunking approach exists to prevent - see
# translate_routes.py's Oracle dialect-prompt entry, which instructs the
# model to always terminate these with a bare "/" so this split can find
# the boundary reliably.
_PLSQL_UNIT_RE = re.compile(
    r'^\s*(?:DECLARE|BEGIN|CREATE(?:\s+OR\s+REPLACE)?\s+(?:PROCEDURE|FUNCTION|'
    r'PACKAGE(?:\s+BODY)?|TRIGGER|TYPE(?:\s+BODY)?))\b',
    re.IGNORECASE,
)


def _split_oracle_chunk(chunk):
    """One "/"-delimited chunk (see _split_oracle_script) -> a list of
    statements ready for cursor.execute(). A PL/SQL unit (_PLSQL_UNIT_RE)
    is returned whole, exactly as written (including its final
    semicolon-after-END, which is syntactically required, not an optional
    separator to strip - see python-oracledb's own anonymous-block example,
    which passes "...end;" straight to execute() unmodified). Anything else
    is ordinary SQL, split by sqlparse.split() same as every other
    backend's execute() here - unchanged behavior for plain statements."""
    stripped = chunk.strip()
    if not stripped:
        return []
    if _PLSQL_UNIT_RE.match(stripped):
        return [stripped]
    return [s.strip() for s in sqlparse.split(chunk) if s.strip()]


# DBMS_OUTPUT capture: DBMS_OUTPUT.PUT_LINE (used throughout PL/SQL for
# progress/status messages - see the model's own generated "write access
# test" blocks, which report success/failure this way, per the dialect
# prompt in translate_routes.py) writes into a session-level buffer that a
# plain cursor.execute() call never sees - nothing surfaces it unless
# something explicitly enables the buffer beforehand and drains it via
# DBMS_OUTPUT.GET_LINES afterward (verified against python-oracledb's own
# "Using DBMS_OUTPUT" docs). Without this, a PL/SQL block's PUT_LINE
# messages vanish silently: the block still runs (and this app's own
# write-test SQL still correctly reports success/failure internally), but
# the user never sees any feedback text in the results tab - they'd see
# "Statement executed successfully. No dataset returned." and nothing else.
_DBMS_OUTPUT_CHUNK_SIZE = 100
# Defensive cap on how many GET_LINES chunks a single statement's drain
# will read - Oracle's own DBMS_OUTPUT buffer is itself bounded (2000
# lines by default; up to 1,000,000 via DBMS_OUTPUT.ENABLE's buffer_size
# argument, not used here), so this can never realistically bind in
# practice - it exists purely so a pathological driver response couldn't
# spin this loop forever.
_DBMS_OUTPUT_MAX_CHUNKS = 1000


def _enable_dbms_output(cursor):
    """Best-effort, called once per execute() call before any statement
    runs - see the module-level comment above. A role without EXECUTE on
    DBMS_OUTPUT (unusual - it's PUBLIC-grantable and virtually always
    available, but not guaranteed) degrades to "no output capture", never
    to a failed script - mirrors get_schema()'s own best-effort try/except
    sections. Also silently absorbs a test/fake cursor with no callproc()
    at all (AttributeError) the same way - existing tests built against
    the generic FakePgCursor (which has no callproc) keep passing
    unmodified, simply never producing a "notices" key."""
    try:
        cursor.callproc("dbms_output.enable")
    except Exception:
        pass


def _drain_dbms_output(cursor):
    """Returns whatever DBMS_OUTPUT.PUT_LINE text has accumulated since
    the last drain (or since _enable_dbms_output(), for the first
    statement) - a list of lines, [] if nothing was written, including
    when _enable_dbms_output() itself silently failed above (GET_LINES on
    a disabled buffer just returns zero lines, not an error - and the same
    AttributeError-swallowing applies here for a callproc-less cursor).
    Uses the batch GET_LINES form (python-oracledb's documented faster
    alternative to looping GET_LINE one line at a time), draining in
    _DBMS_OUTPUT_CHUNK_SIZE-sized chunks until a short chunk signals the
    buffer is empty - exactly the pattern shown in python-oracledb's own
    "Using DBMS_OUTPUT" docs."""
    lines = []
    try:
        lines_var = cursor.arrayvar(str, _DBMS_OUTPUT_CHUNK_SIZE)
        num_lines_var = cursor.var(int)
        for _ in range(_DBMS_OUTPUT_MAX_CHUNKS):
            num_lines_var.setvalue(0, _DBMS_OUTPUT_CHUNK_SIZE)
            cursor.callproc("dbms_output.get_lines", (lines_var, num_lines_var))
            num_lines = num_lines_var.getvalue()
            lines.extend(lines_var.getvalue()[:num_lines])
            if num_lines < _DBMS_OUTPUT_CHUNK_SIZE:
                break
    except Exception:
        # Never let a best-effort output capture turn an otherwise-
        # successful statement into a failure - see _enable_dbms_output's
        # docstring.
        pass
    return lines


def _split_oracle_script(sql_text):
    """The whole script -> statements ready for cursor.execute(). First
    splits on a bare "/" line (Oracle's own PL/SQL-block boundary marker -
    see _SLASH_TERMINATOR_RE), then splits each resulting chunk via
    _split_oracle_chunk. A PL/SQL block/body with no trailing "/" (the
    model didn't follow the dialect-prompt instruction, or it's the last
    thing in the script) still comes out right: it's simply the last chunk,
    running to the end of the text, and _split_oracle_chunk's PL/SQL check
    still recognizes and returns it whole."""
    statements = []
    pos = 0
    for match in _SLASH_TERMINATOR_RE.finditer(sql_text):
        statements.extend(_split_oracle_chunk(sql_text[pos:match.start()]))
        pos = match.end()
    statements.extend(_split_oracle_chunk(sql_text[pos:]))
    return statements


def _named_in_params(prefix, values):
    """(fragment, params) for a dynamic IN (...) clause under the
    connector's "named" paramstyle (:name, not %s/?) - e.g. for
    values=["a", "b"] and prefix="t", returns (":t0, :t1", {"t0": "a",
    "t1": "b"}). Used wherever get_schema() below needs to scope a query
    to the bounded kept_names set (see backends/base.py)."""
    names = [f"{prefix}{i}" for i in range(len(values))]
    fragment = ", ".join(f":{n}" for n in names)
    return fragment, dict(zip(names, values))


# Phase 2 (deep-only) sampling: which ALL_TAB_COLUMNS.data_type strings are
# worth a MIN()/MAX() range query (numeric/date-ish) vs a frequent-value
# GROUP BY (bounded/categorical-ish) - see get_schema()'s "Column value
# samples" section below. Deliberately conservative/small lists rather than
# "everything that isn't the other list", same reasoning as backends/
# postgres.py's own NUMERIC_OR_DATE_TYPES/CATEGORICAL_TYPES, just spelled for
# Oracle's own type names. TIMESTAMP columns carry a precision suffix (e.g.
# "TIMESTAMP(6)", "TIMESTAMP(6) WITH TIME ZONE") - matched via a prefix check
# (_is_numeric_or_date_type below) rather than exact set membership, since
# the precision digit (and WITH [LOCAL] TIME ZONE suffix) varies per column.
NUMERIC_OR_DATE_TYPES = frozenset({
    "NUMBER", "FLOAT", "INTEGER", "BINARY_FLOAT", "BINARY_DOUBLE", "DATE",
})
CATEGORICAL_TYPES = frozenset({"VARCHAR2", "CHAR", "NCHAR", "NVARCHAR2", "VARCHAR"})


def _is_numeric_or_date_type(data_type):
    return data_type in NUMERIC_OR_DATE_TYPES or (data_type or "").startswith("TIMESTAMP")


# Bounds on Phase 2's per-table sampling cost - same values, same rationale,
# as backends/postgres.py's own MAX_COLUMNS_FOR_SAMPLING/
# MAX_NUMERIC_COLUMNS_FOR_MINMAX/MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE/
# FREQUENT_VALUES_LIMIT: MAX_COLUMNS_FOR_SAMPLING skips per-column sampling
# entirely for a table wider than this (still gets a live row count);
# MAX_NUMERIC_COLUMNS_FOR_MINMAX bounds one table's combined MIN()/MAX()
# SELECT list; MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE bounds how many
# separate "frequent values" GROUP BY queries one table gets.
MAX_COLUMNS_FOR_SAMPLING = 25
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
# FREQUENT_VALUES_LIMIT now imported from backends/base.py (env-configurable
# via SCHEMA_FREQUENT_VALUES_LIMIT) rather than defined here - see that
# module's own comment. Oracle 12c+ "FETCH FIRST n ROWS ONLY" (this file's
# table-name-scan query already relies on this same syntax for scan_limit)
# is used in place of a LIMIT clause, which Oracle has no equivalent of.


def _quote_ident(name):
    """Double-quotes an Oracle identifier for interpolation into a plain SQL
    string (escaping an embedded '"'), for the Phase 2 per-table live-query
    section below - mirrors backends/postgres.py's own _quote_ident (the
    same quoting rule Oracle itself uses for a case-preserving identifier).
    `name` always comes from ALL_TABLES/ALL_TAB_COLUMNS data this same
    connection already queried in Phase 1 (kept_names / column names) -
    never raw user input - so this only needs to be correct, not defend
    against adversarial identifiers."""
    return '"' + str(name).replace('"', '""') + '"'


def _is_near_unique_column(num_distinct, num_rows):
    """Gate for whether a categorical column's "frequent values" sample is
    worth rendering at all. ALL_TAB_COL_STATISTICS.NUM_DISTINCT (an
    optimizer statistic DBMS_STATS computes - no live scan) is a plain
    estimated distinct-value COUNT, unlike Postgres's pg_stats.n_distinct
    (which already encodes a signed distinct/total ratio) - so "near-unique"
    is computed here as num_distinct / num_rows, using num_rows from
    ALL_TABLES.NUM_ROWS (also a free optimizer stat, already fetched in
    Phase 1 - see _build_shallow_schema_parts). >= 0.5 mirrors the same
    threshold backends/postgres.py's own _is_near_unique_n_distinct uses for
    its ratio branch (n_distinct <= -0.5, i.e. >=50% of rows have a distinct
    value).

    None for num_distinct (stats never gathered for this column) is treated
    permissively as "not near-unique", matching Postgres's own None
    handling - the alternative would silently hide sampling for a freshly
    created/unanalyzed table forever. When num_rows is itself unknown
    (table-level stats never gathered either), falls back to treating a
    large absolute NUM_DISTINCT (>1000) as near-unique - the same threshold
    Postgres's own n_distinct-as-absolute-count branch uses.

    Deliberately does NOT fall back to a live COUNT(DISTINCT col) scan when
    ALL_TAB_COL_STATISTICS has nothing for a column (see the plan this
    implements, which allows either choice): that would add yet another
    per-column live query on top of the min/max and frequent-value queries
    this same table already pays for, just to handle the already-rare case
    of a table DBMS_STATS was never pointed at - a case where being
    slightly over-eager about sampling is a minor cost, not a correctness
    problem, unlike the query-count blowup a live fallback would risk on a
    schema with many never-analyzed tables."""
    if num_distinct is None:
        return False
    if num_rows:
        return (num_distinct / num_rows) >= 0.5
    return num_distinct > 1000


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
            "tcp_connect_timeout": float(resolve_timeout_seconds(
                descriptor, "connect_timeout_seconds", DB_CONNECT_TIMEOUT_SECONDS,
            )),
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

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - mirrors backends/postgres.py's own
        _build_shallow_schema_parts (see its docstring for the general
        shape) and backends/base.py's Backend.get_schema()/
        get_schema_shallow() docstrings for why this split exists at all.

        Returns None if the connection's current schema/owner has no table
        at all (mirrors the pre-split get_schema()'s "return None" for that
        case). Otherwise returns (schema_parts, table_columns, phase2_ctx):
          - schema_parts: the ordered list of text sections, not yet
            joined/capped - identical in kind to what get_schema() used to
            build directly, just returned before the final cap_schema_text()
            call.
          - table_columns: {table_name: [column_name, ...]}, scoped to the
            same bounded kept_names set schema_parts describes - handed to
            the shared find_naming_convention_relationships() helper by
            get_schema()'s Phase 2 pass (no extra query needed for that).
          - phase2_ctx: raw, already-fetched data Phase 2 wants to reuse
            without re-querying - kept_names/column_types (for deciding what
            to sample), num_rows_by_table (ALL_TABLES.NUM_ROWS, already
            fetched below - reused as the row-count baseline
            _is_near_unique_column() needs), and the raw views rows (so
            get_schema() can render full view body text without a second
            trip to the database - see the "Views" section below for why
            only the view *name* is rendered here). There is no routines
            entry: Oracle's ALL_PROCEDURES/ALL_ARGUMENTS carry no reusable
            body text at all - see the "Routines" section below.
        """
        schema_parts = []
        table_columns = {}
        column_types = {}

        with connection.cursor() as cursor:
            # Phase 1: cheap - just the distinct table names (+ NUM_ROWS,
            # new - see "Row count estimates" below, folded into this same
            # query since it's already a column of ALL_TABLES, the exact
            # view this query already reads - no extra round trip needed),
            # bounded so a schema with an extreme number of tables can't
            # make even this scan unbounded (SCHEMA_MAX_TABLE_NAMES_SCANNED).
            # Grouped into date-shard families and capped to
            # SCHEMA_MAX_TABLES entries (see backends/base.py) before any
            # column/constraint/view query runs, same staging as every
            # other backend's get_schema(). Scoped to
            # SYS_CONTEXT('USERENV','CURRENT_SCHEMA') rather than a
            # hardcoded owner name.
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
                SELECT table_name, num_rows
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
            all_rows = cursor.fetchall()
            all_table_names = [row[0] for row in all_rows]
            num_rows_by_table = {row[0]: row[1] for row in all_rows}

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
            #
            # Identity marker (new) - LEFT JOINed against
            # ALL_TAB_IDENTITY_COLS and folded into this same unconditional
            # query, the same way Postgres's own is_identity/
            # identity_generation columns are (see backends/postgres.py's
            # _build_shallow_schema_parts) rather than a separate query.
            # Safe to leave unconditional (not try/except-wrapped): both
            # identity columns and ALL_TAB_IDENTITY_COLS are Oracle 12c+
            # features, and this file already assumes 12c+ elsewhere (the
            # FETCH FIRST syntax just above has no pre-12c equivalent this
            # app would fall back to).
            in_fragment, in_params = _named_in_params("t", kept_names)
            cursor.execute(f"""
                SELECT c.table_name, c.column_name, c.data_type, c.nullable,
                       i.generation_type
                FROM all_tab_columns c
                LEFT JOIN all_tab_identity_cols i
                  ON i.owner = c.owner
                 AND i.table_name = c.table_name
                 AND i.column_name = c.column_name
                WHERE c.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                  AND c.table_name IN ({in_fragment})
                ORDER BY c.table_name, c.column_id
            """, in_params)
            columns_data = cursor.fetchall()

            tables = {}
            for table_name, col_name, data_type, nullable, generation_type in columns_data:
                table_columns.setdefault(table_name, []).append(col_name)
                column_types.setdefault(table_name, {})[col_name] = data_type
                identity_str = f" IDENTITY ({generation_type})" if generation_type else ""
                tables.setdefault(table_name, []).append(
                    f"  {col_name} {data_type} "
                    f"{'NULL' if nullable == 'Y' else 'NOT NULL'}{identity_str}"
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
            # rather than a full DDL dump (unchanged by this pass - see
            # module docstring; DBMS_METADATA.GET_DDL is a bigger,
            # out-of-scope change, not a fix applied here). TEXT_VC doesn't
            # exist on every Oracle version this app might connect to, so
            # this is best-effort like the constraints section above - a
            # version without it just skips this section.
            #
            # Shallow rendering is name-only (no TEXT_VC body) - the raw
            # rows (including each view's possibly-truncated body text) are
            # still fetched here (one query, reused by both phases) and
            # threaded through via phase2_ctx below so get_schema() (deep)
            # can render the full body without a second query - mirrors
            # backends/postgres.py's own Views/"View definitions" split.
            views = []
            try:
                cursor.execute("""
                    SELECT view_name, text_vc
                    FROM all_views
                    WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                """)
                views = cursor.fetchall()
                if views:
                    schema_parts.append(
                        "Views:\n" + "\n".join(f"  View {t}" for (t, _d) in views)
                    )
            except Exception:
                pass

            # 4. Comments (new) - table and column comments via Oracle's own
            # ALL_TAB_COMMENTS/ALL_COL_COMMENTS dictionary views, scoped to
            # the current schema/owner and kept_names. Best-effort/
            # try-except, like every new optional section below (mirrors
            # backends/postgres.py's own Comments section): a role that
            # somehow can't evaluate these still gets every other section,
            # rather than losing the whole schema fetch over one cosmetic
            # addition. The same `:t0, :t1, ...` bind names from in_params
            # are reused across both UNION ALL branches below - Oracle
            # resolves a repeated named bind to the same bound value
            # everywhere it appears in one statement, so in_params doesn't
            # need to be duplicated under different names.
            try:
                cursor.execute(f"""
                    SELECT * FROM (
                        SELECT table_name AS tbl, CAST(NULL AS VARCHAR2(128)) AS col, comments
                        FROM all_tab_comments
                        WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                          AND table_name IN ({in_fragment})
                        UNION ALL
                        SELECT table_name, column_name, comments
                        FROM all_col_comments
                        WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                          AND table_name IN ({in_fragment})
                    )
                    ORDER BY tbl, col
                """, in_params)
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

            # 5. Row count estimates (new) - ALL_TABLES.NUM_ROWS, already
            # fetched above (the table-name query) rather than re-queried -
            # an optimizer stat (last DBMS_STATS run's estimate, not a live
            # scan; NULL if stats were never gathered for that table - see
            # get_schema()'s "Live row counts" section for the
            # authoritative, deep-only counterpart). A NULL/never-analyzed
            # table is skipped rather than rendered as a misleading "~None
            # rows".
            estimate_lines = [
                f"  {t}: ~{int(num_rows_by_table[t])} rows (estimate)"
                for t in kept_names
                if num_rows_by_table.get(t) is not None
            ]
            if estimate_lines:
                schema_parts.append("Row count estimates:\n" + "\n".join(estimate_lines))

            # 6. Routines (new) - existence + signature only, no body, ever
            # (not just in the shallow fetch - see get_schema() below).
            # Unlike every ANSI-information_schema dialect (which exposes a
            # ready-made routine_definition/ROUTINE_DEFINITION text column),
            # Oracle's ALL_PROCEDURES/ALL_ARGUMENTS carry no body text at
            # all - the only way to get a routine's source is ALL_SOURCE
            # (line-by-line PL/SQL text, reconstructed by concatenation),
            # which is exactly the kind of package/body introspection this
            # pass deliberately doesn't take on (see the plan this
            # implements: "don't over-engineer package introspection"). So
            # there is no "Routine definitions" Phase 2 section for Oracle -
            # only the Views section has a full-body deep counterpart.
            #
            # Scoped to standalone procedures/functions only
            # (PROCEDURE_NAME IS NULL excludes package members - a package
            # member's "procedure name" in ALL_PROCEDURES is its own name
            # inside the package, not NULL - so packages themselves stay
            # out of scope per the plan). ALL_ARGUMENTS is joined to build
            # each routine's parameter list (DATA_LEVEL = 0 excludes nested
            # record/table-type argument members; PACKAGE_NAME IS NULL
            # matches the same standalone-only scope); a function's return
            # value is its own "argument" row with ARGUMENT_NAME IS NULL
            # and POSITION = 0, picked out in the Python loop below rather
            # than via a second query.
            try:
                cursor.execute("""
                    SELECT p.object_name, p.object_type, a.argument_name, a.data_type, a.position
                    FROM all_procedures p
                    LEFT JOIN all_arguments a
                      ON a.owner = p.owner
                     AND a.object_name = p.object_name
                     AND a.package_name IS NULL
                     AND a.data_level = 0
                    WHERE p.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                      AND p.object_type IN ('FUNCTION', 'PROCEDURE')
                      AND p.procedure_name IS NULL
                    ORDER BY p.object_name, a.position
                """)
                routine_rows = cursor.fetchall()
                routines_map = {}
                for obj_name, obj_type, arg_name, data_type, position in routine_rows:
                    entry = routines_map.setdefault(
                        obj_name, {"type": obj_type, "params": [], "return": None}
                    )
                    if arg_name is None:
                        if obj_type == "FUNCTION" and position == 0:
                            entry["return"] = data_type
                    else:
                        entry["params"].append(f"{arg_name} {data_type}")
                if routines_map:
                    routine_lines = []
                    for name in sorted(routines_map):
                        entry = routines_map[name]
                        sig = f"  {name}({', '.join(entry['params'])})"
                        if entry["return"]:
                            sig += f" -> {entry['return']}"
                        routine_lines.append(sig)
                    schema_parts.append("Routines:\n" + "\n".join(routine_lines))
            except Exception:
                pass

            # 7. Session facts (new) - one line for the whole connection,
            # not per-table. SESSIONTIMEZONE is the session's effective
            # timezone; NLS_TERRITORY/NLS_SORT (read from
            # NLS_SESSION_PARAMETERS) are Oracle's rough equivalent of a
            # default collation - there's no single "collation" concept in
            # Oracle the way ANSI SQL/Postgres has one: NLS_SORT governs
            # linguistic string comparison/ordering (the role a Postgres
            # collation plays), and NLS_TERRITORY additionally influences
            # locale-dependent defaults (date/number formatting) alongside
            # it.
            try:
                cursor.execute("""
                    SELECT SESSIONTIMEZONE,
                           (SELECT value FROM nls_session_parameters WHERE parameter = 'NLS_TERRITORY'),
                           (SELECT value FROM nls_session_parameters WHERE parameter = 'NLS_SORT')
                    FROM DUAL
                """)
                row = cursor.fetchone()
                if row:
                    tz, territory, sort_order = row
                    schema_parts.append(
                        f"Session: timezone={tz}; territory={territory}; sort={sort_order}"
                    )
            except Exception:
                pass

            # 8. Grants (new) - ALL_TAB_PRIVS, scoped to the current
            # schema/owner (TABLE_SCHEMA) and kept_names.
            #
            # Re-examining this module's own long-standing "Deliberately no
            # Indexes/Triggers/Grants sections ... not verified against a
            # real Oracle instance" caveat (previously right here, now
            # updated - see below): that caveat was about never having
            # written ANY grants query yet, not about ALL_TAB_PRIVS
            # specifically being unsafe. ALL_TAB_PRIVS is a standard,
            # always-present data-dictionary view (like every other ALL_*
            # view this file already queries), it's inherently
            # current-user-scoped by Oracle itself (it only ever shows
            # privileges the connected user can actually see - grants made
            # BY or TO them, or on objects they own - never another
            # schema's private grant graph, so there's no risk of leaking
            # more than the connected role could already see via SQL*Plus),
            # and it's wrapped in this same try/except convention as every
            # other optional section here - so a permissions edge case
            # degrades to "skip this section" exactly like Constraints/
            # Views above, never a failed schema fetch. That resolves the
            # caveat in favor of adding a minimal grants query now, rather
            # than leaving it deferred a second time (the plan this
            # implements explicitly allows either choice here - this is the
            # "add it safely" branch, not an override of the original
            # reasoning).
            #
            # Indexes/Triggers remain out of scope for this pass (neither
            # attribute is in the plan's Phase 1/Phase 2 tables this change
            # implements) - still left for a future follow-up, unchanged
            # from before.
            try:
                cursor.execute(f"""
                    SELECT grantee, table_name, privilege
                    FROM all_tab_privs
                    WHERE table_schema = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                      AND table_name IN ({in_fragment})
                    ORDER BY table_name, grantee
                """, in_params)
                grant_rows = cursor.fetchall()
                if grant_rows:
                    grant_lines = [f"  Grant {priv} on {t} to {g}" for (g, t, priv) in grant_rows]
                    schema_parts.append("Grants:\n" + "\n".join(grant_lines))
            except Exception:
                pass

            # 9. External tables (new) - ALL_EXTERNAL_TABLES, cleanly
            # introspectable, scoped to kept_names. No RLS/masking
            # equivalent is added here: Oracle's RLS (VPD, Virtual Private
            # Database) is enforced by a security policy FUNCTION attached
            # at runtime via DBMS_RLS, not a simple per-table catalog flag
            # the way Postgres's pg_class.relrowsecurity is - there is no
            # reliable "is RLS enabled on this table" catalog column to
            # read, and fabricating a heuristic (e.g. "a policy-shaped
            # function with this naming convention exists") risks a false
            # negative/positive that would actively mislead query
            # generation, which is worse than omitting it entirely - per
            # the plan's own explicit caution on this exact point. So this
            # section reports external tables only, never RLS, and is
            # titled accordingly rather than reusing Postgres's combined
            # "Row-level security / federation" heading.
            try:
                cursor.execute(f"""
                    SELECT table_name
                    FROM all_external_tables
                    WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                      AND table_name IN ({in_fragment})
                """, in_params)
                external_rows = cursor.fetchall()
                if external_rows:
                    ext_lines = [f"  {row[0]}: [external table]" for row in external_rows]
                    schema_parts.append("External tables:\n" + "\n".join(ext_lines))
            except Exception:
                pass

        phase2_ctx = {
            "kept_names": kept_names,
            "column_types": column_types,
            "num_rows_by_table": num_rows_by_table,
            "views": views,
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
        """Phase 1 (catalog-only, via _build_shallow_schema_parts) plus
        Phase 2 (live queries: full view body text, cardinality-gated
        sampling, live row counts, naming-convention relationships) - see
        backends/base.py's Backend.get_schema() docstring."""
        built = self._build_shallow_schema_parts(connection)
        if built is None:
            return None
        schema_parts, table_columns, phase2_ctx = built
        schema_parts = list(schema_parts)

        kept_names = phase2_ctx["kept_names"]
        column_types = phase2_ctx["column_types"]
        num_rows_by_table = phase2_ctx["num_rows_by_table"]
        views = phase2_ctx["views"]

        # Dataset size estimate (new, deep-only) - unlike the per-table
        # "Live row counts" section below (scoped to kept_names, a capped
        # subset of at most SCHEMA_MAX_TABLES tables), this covers the
        # whole schema. Row count needs no new query/try-except: Phase 1
        # already scanned every table (up to SCHEMA_MAX_TABLE_NAMES_SCANNED)
        # into num_rows_by_table via ALL_TABLES, so summing that dict (pure
        # Python, can't fail from a DB call) gives the true schema-wide
        # total. Byte size has no equivalent free Phase-1 value, so it's a
        # separate best-effort query - USER_SEGMENTS access/contents can
        # vary by grants, hence its own try/except.
        total_rows = sum(v for v in num_rows_by_table.values() if v is not None)
        total_tables = len(num_rows_by_table)
        if total_tables:
            total_bytes = None
            try:
                with connection.cursor() as cursor_size:
                    cursor_size.execute("""
                        SELECT SUM(bytes) FROM user_segments
                        WHERE segment_type IN ('TABLE', 'TABLE PARTITION')
                    """)
                    size_row = cursor_size.fetchone()
                    if size_row is not None:
                        total_bytes = size_row[0]
            except Exception:
                pass
            size_line = format_dataset_size_line(
                total_rows=total_rows, total_bytes=total_bytes,
            )
            if size_line:
                schema_parts.append(size_line)

        # Full view bodies (deep-only) - reusing the raw rows
        # _build_shallow_schema_parts already fetched (TEXT_VC, possibly
        # truncated per the module docstring's accepted tradeoff) - no
        # re-query. No "Routine definitions" counterpart exists for Oracle
        # - see _build_shallow_schema_parts' Routines section above for why.
        if views:
            schema_parts.append(
                "View definitions:\n" + "\n".join(
                    f"  View {t}: {format_multiline_schema_entry_body(d)}" for (t, d) in views
                )
            )

        with connection.cursor() as cursor:
            # Cardinality gate for the frequent-value sampling below -
            # ALL_TAB_COL_STATISTICS.NUM_DISTINCT is a planner/optimizer
            # statistic Oracle already computes via DBMS_STATS (no live
            # scan) - see _is_near_unique_column()'s docstring for exactly
            # what this gates, why num_rows_by_table (from Phase 1) is
            # needed alongside it, and why this deliberately doesn't fall
            # back to a live COUNT(DISTINCT ...) scan.
            distinct_stats = {}
            if kept_names:
                try:
                    stats_fragment, stats_params = _named_in_params("t", kept_names)
                    cursor.execute(f"""
                        SELECT table_name, column_name, num_distinct
                        FROM all_tab_col_statistics
                        WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                          AND table_name IN ({stats_fragment})
                    """, stats_params)
                    for tbl, col, num_distinct in cursor.fetchall():
                        distinct_stats.setdefault(tbl, {})[col] = num_distinct
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
                # NUM_ROWS estimate above (free but stale until the next
                # DBMS_STATS run). Kept as `row_count` - see postgres.py's
                # identical comment - so the "frequent values" sampling
                # further down can size its min_frequent_value_count()
                # floor off this fresh count rather than the possibly-stale
                # num_rows_by_table estimate already used for the near-
                # unique gate just below.
                row_count = None
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {_quote_ident(table_name)}")
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
                    c for c, t in col_types.items() if _is_numeric_or_date_type(t)
                ][:MAX_NUMERIC_COLUMNS_FOR_MINMAX]
                categorical_cols = [c for c, t in col_types.items() if t in CATEGORICAL_TYPES]

                table_sample_lines = []

                # Min/max, all eligible numeric/date columns in one combined
                # query per table (bounded by MAX_NUMERIC_COLUMNS_FOR_MINMAX)
                # rather than one query per column.
                if numeric_cols:
                    try:
                        select_parts = ", ".join(
                            f"MIN({_quote_ident(c)}), MAX({_quote_ident(c)})" for c in numeric_cols
                        )
                        cursor.execute(f"SELECT {select_parts} FROM {_quote_ident(table_name)}")
                        row = cursor.fetchone()
                        if row is not None:
                            for i, c in enumerate(numeric_cols):
                                min_v, max_v = row[2 * i], row[2 * i + 1]
                                table_sample_lines.append(f"    {c}: range [{min_v} .. {max_v}]")
                    except Exception:
                        pass

                # Frequent values - one query per eligible categorical
                # column (capped at MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE
                # per table), skipping any column ALL_TAB_COL_STATISTICS
                # says is close to unique (see _is_near_unique_column).
                num_rows = num_rows_by_table.get(table_name)
                eligible_categorical = []
                for c in categorical_cols:
                    num_distinct = distinct_stats.get(table_name, {}).get(c)
                    if _is_near_unique_column(num_distinct, num_rows):
                        continue
                    eligible_categorical.append(c)
                    if len(eligible_categorical) >= MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE:
                        break

                # HAVING floor - see backends/base.py's
                # min_frequent_value_count() docstring and postgres.py's
                # identical use of it.
                min_count = min_frequent_value_count(row_count)
                having_clause = f"HAVING COUNT(*) >= {min_count} " if min_count is not None else ""
                for c in eligible_categorical:
                    try:
                        cursor.execute(
                            f"SELECT {_quote_ident(c)}, COUNT(*) FROM {_quote_ident(table_name)} "
                            f"GROUP BY {_quote_ident(c)} {having_clause}"
                            f"ORDER BY COUNT(*) DESC "
                            f"FETCH FIRST {FREQUENT_VALUES_LIMIT} ROWS ONLY"
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

        statements = _split_oracle_script(sql_text)
        results = []

        with connection.cursor() as cursor:
            _enable_dbms_output(cursor)
            for stmt in statements:
                if _PLSQL_UNIT_RE.match(stmt):
                    # A PL/SQL block/body must reach Oracle exactly as
                    # written, including its final semicolon-after-END -
                    # see _split_oracle_chunk's docstring.
                    stmt_clean = stmt
                else:
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
                    # Drained AFTER this statement's own columns/rows/count
                    # are already captured above, so a subsequent
                    # dbms_output.get_lines callproc() (which sets the
                    # cursor's own description/rowcount as a side effect,
                    # like any statement) can never clobber THIS
                    # statement's result - see _drain_dbms_output's
                    # docstring. Only ever attached when non-empty, so
                    # every other backend's/existing test's result-dict
                    # shape (no "notices" key) is unaffected.
                    notices = _drain_dbms_output(cursor)
                    if notices:
                        result_entry['notices'] = notices
                    results.append(result_entry)
                except Exception as e:
                    # Don't let a mid-script failure silently drop every
                    # result already collected in `results` - see
                    # SqlExecutionError's docstring in backends/base.py.
                    raise SqlExecutionError(str(e), results, stmt_clean, len(results), len(statements)) from e

        return results
