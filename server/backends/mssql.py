"""
backends/mssql.py

MssqlBackend: talks to Microsoft SQL Server (on-prem, or Azure SQL
Database) via python-tds (import name `pytds`) - a pure-Python
implementation of the TDS wire protocol, with zero system dependencies.
Chosen over `pymssql` (needs FreeTDS system libraries) and `pyodbc` (needs
unixODBC plus Microsoft's own msodbcsql driver, which requires adding
Microsoft's apt repo/signing key) specifically because every other
non-Postgres/MySQL dialect added to this app so far - oracledb (thin
mode), PyMySQL, databricks-sql-connector, snowflake-connector-python -
needed no Dockerfile changes to add, and `python-tds` is the one SQL
Server driver that keeps that streak: it ships as a plain `py3-none-any`
wheel. TLS support additionally needs `pyOpenSSL` (also a pure wheel) and,
for a sensible default trust store, `certifi`.

Mirrors backends/redshift.py's shape more than backends/oracle.py's for
schema introspection (SQL Server, like Redshift/Postgres, implements a
substantial ANSI-standard INFORMATION_SCHEMA - unlike Oracle, which has no
information_schema at all and needs its own ALL_* catalog views), but
borrows Oracle's opt-in TLS boolean flag pattern for the "encrypt"
descriptor field, since - like Oracle Cloud vs. an on-prem/XE listener -
some SQL Server deployments (Azure SQL Database) require encryption while
others (a bare on-prem box) may not have it configured at all.

A SQL Server descriptor looks like:
    {"type": "mssql", "host": "...", "port": 1433, "database": "...",
     "user": "...", "password": "...", "schema": "...", "encrypt": true}
"host"/"database"/"user"/"password" are required; "port" defaults to SQL
Server's standard port (1433) when omitted. "schema" is optional - unlike
Oracle's ALTER SESSION SET CURRENT_SCHEMA or Redshift's/Postgres's SET
search_path, T-SQL has no single, version-stable statement to change a
session's default schema, so connect() below does NOT attempt any session
mutation for "schema" at all. Instead, every INFORMATION_SCHEMA query in
get_schema() is scoped by `TABLE_SCHEMA = COALESCE(%s, SCHEMA_NAME())`,
binding the descriptor's schema (or NULL) directly - SCHEMA_NAME() is SQL
Server's own built-in returning the connecting login's default schema
(commonly "dbo") when no override is supplied. This is a deliberate
adaptation of Oracle's/Redshift's "optional namespace override" pattern to
a dialect that genuinely has no session-level equivalent, not a missed
step.

Because nothing changes at the session level, an unqualified table
reference in generated SQL resolves against the connecting login's own
default schema, not this descriptor's "schema" override - if those two
differ (the common reason to set "schema" explicitly in the first place),
an unqualified query silently targets the wrong place and fails with
"Invalid object name" - the generated SQL never knew which schema it
actually needed to land in. get_schema() below therefore renders every
table/view name schema-qualified (e.g. "reporting.customers") whenever an
explicit override is configured, so a query built from this schema text
carries the right
qualification by construction rather than depending on
translate_routes.py's dialect prompt to explain the rule correctly (see
that file's _DIALECT_PROMPT_INTROS entry for this dialect). Left
unqualified when no override is configured, since in that case an
unqualified reference already resolves correctly, into the same default
schema this introspection itself just queried via SCHEMA_NAME().

This first pass is deliberately narrow, mirroring how every other
non-ambient-identity dialect's (Snowflake/Databricks/Oracle/Redshift) own
first pass was narrowed too:
- Only SQL Login (username/password) authentication is supported. Windows
  Authentication and Azure AD/Entra ID auth (both meaningfully more
  machinery - Kerberos/NTLM negotiation, or OAuth token acquisition and
  refresh) are deferred follow-up work, not built into this first pass.
- "encrypt" (bool, defaults to True when the field is absent entirely -
  unlike Oracle's "ssl", which defaults to off at this layer and is only
  pre-checked at the UI layer) turns TLS on. pytds's own encryption model
  requires handing it a CA bundle file (`cafile`) to validate the server's
  certificate against - there's no simple "encrypt without validating"
  toggle the way sslmode=require is for Postgres/Redshift. To keep the
  descriptor a single boolean (matching every other dialect's simple
  opt-in flags, with no separate cert-upload field in the UI), connect()
  below defaults `cafile` to certifi's bundled public CA list whenever
  encrypt is true. This correctly covers the realistic common cases
  (Azure SQL Database's public certificate, or an on-prem box with a
  certificate issued by a real/enterprise CA that chains to a public
  root). A fully private/self-signed CA genuinely can't be validated
  through a plain boolean checkbox - that's a real first-pass limitation,
  not silently glossed over, mirroring how Databricks' PAT-only/no-OAuth
  and Oracle's no-wallet/mTLS limitations are each called out plainly in
  their own module docstrings.
- Indexes/Triggers sections are still deferred from get_schema() below -
  SQL Server supports both (sys.indexes, sys.triggers) and they could be
  added later, but every dialect that could support "nice to have"
  introspection extras deferred at least one of them in its own first pass
  too (Oracle deferred Grants; Redshift deferred Indexes/Triggers/Grants) -
  this isn't a new gap, it's the same "ship core Tables/Columns/
  Constraints/Views, defer the rest" precedent. Neither attribute was on
  the two-phase schema-introspection plan's requested list (only Grants
  was), so this narrower scope is deliberate, not an oversight.

  Grants, unlike Indexes/Triggers, WAS on that plan's requested list, and
  is no longer deferred: the original deferral note above (when this
  module was first written) never argued the query itself was unsafe or
  uncertain to write - it only said "ship the core sections first, the
  same way every other dialect narrowed its own first pass." That's a
  scope decision, not a correctness concern, so it doesn't block adding
  Grants now that it's explicitly asked for. SQL Server's
  INFORMATION_SCHEMA.TABLE_PRIVILEGES is the same ANSI-standard,
  already-working shape backends/mysql.py's own Grants section already
  uses (MySQL has no role_table_grants either) - already-visible-to-the-
  caller semantics, no elevated catalog access needed, wrapped in the same
  try/except every other optional section here uses. See
  _build_shallow_schema_parts()'s "Grants" section below.

Which of "password" must never round-trip back to the frontend once saved
is state_store.py's _CREDENTIAL_CONFIG_FIELDS' responsibility - "password"
is already covered there (shared with Oracle's/Redshift's own standalone
"password" field), no new field name needed.

Unlike Redshift/Snowflake/Databricks, SQL Server DOES enforce PK/FK/UNIQUE
constraints at write time - the Constraints section below says so
explicitly, rather than reusing Redshift's "declared only, never enforced"
wording, since that caveat would be actively wrong for this dialect.

The driver's DB-API paramstyle is "pyformat" (confirmed against the
installed package, not assumed) - cursor.execute() accepts plain
positional `%s` placeholders with a tuple of params, same substitution
style PyMySQL uses, so the dynamic IN (...) clause below is built the same
way backends/mysql.py's is (a `%s`-per-item format string plus a flat
params tuple), not Oracle's hand-rolled `:name` binding.

NOTE for reviewers: like every other non-Postgres/MySQL backend here, this
has been exercised against the fake DB-API harness in
tests/server/helpers.py, not a real SQL Server instance yet - treat the
constraint-resolution query (which joins TABLE_CONSTRAINTS/
KEY_COLUMN_USAGE/REFERENTIAL_CONSTRAINTS to resolve FK targets, the
standard pattern for SQL Server's ANSI-compliant information_schema) as a
solid first draft to validate against a real instance before relying on it.

pytds/pyOpenSSL compatibility note: pytds's own TLS hostname check
(pytds.tls.validate_host) calls pyOpenSSL's X509.get_extension(index) to
walk a peer certificate's extensions - that method was removed from
pyOpenSSL in 26.2.0 (present-but-deprecated in 26.1.0, gone by 26.2.0;
confirmed directly against the installed package's source, not assumed),
so any encrypt=true connection (the default - see connect() below) fails
with "'X509' object has no attribute 'get_extension'" once pyOpenSSL is at
26.2.0 or newer. Pinning pyOpenSSL back down is not a safe fix in this
app: it would force downgrading "cryptography" too (pyOpenSSL<26.2
requires cryptography<49, <26.1 requires <48), and "cryptography" is
shared with google-auth/oracledb/snowflake-connector-python/pdfminer.six
elsewhere in this codebase - too wide a blast radius for what's really a
one-function incompatibility. Instead, importing this module replaces
pytds.tls.validate_host with an equivalent built on the "cryptography"
library's own X.509 API (pyOpenSSL's X509.to_cryptography() still works
fine on every version - verified against a real self-signed cert built
with "cryptography" and round-tripped through pyOpenSSL). This is a
drop-in replacement, not a security downgrade: it checks the same CN and
subjectAltName DNS entries (including the same single-label wildcard
support pytds's own version has) that pytds's original implementation
did - unlike the alternative of passing validate_host=False to pytds's
connect(), which would skip hostname verification altogether while still
only checking that some CA-trusted certificate was presented.
"""

import concurrent.futures

import certifi
import pytds
import pytds.tls
import sqlparse
from cryptography import x509 as _crypto_x509
from cryptography.x509.oid import ExtensionOID as _ExtensionOID, NameOID as _NameOID

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS, resolve_timeout_seconds,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text, fetch_capped_rows,
    find_naming_convention_relationships,
)


# Phase 2 (deep-only) sampling: which INFORMATION_SCHEMA.COLUMNS.DATA_TYPE
# strings (SQL Server reports these lowercase, e.g. "int", "nvarchar") are
# worth a MIN()/MAX() range query (numeric/date-ish) vs a frequent-value
# GROUP BY (bounded/categorical-ish) - see get_schema()'s "Column value
# samples" section below. Same conservative-allowlist posture as
# backends/postgres.py's own two lists: a data_type this doesn't recognize
# (e.g. "xml", "geography", "varbinary") is simply skipped for sampling
# rather than guessed at.
NUMERIC_OR_DATE_TYPES = frozenset({
    "tinyint", "smallint", "int", "bigint", "decimal", "numeric", "float", "real",
    "money", "smallmoney",
    "date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time",
})
CATEGORICAL_TYPES = frozenset({
    "char", "varchar", "nchar", "nvarchar", "text", "ntext", "uniqueidentifier", "bit",
})

# Bounds on Phase 2's per-table sampling cost - see get_schema()'s "Column
# value samples" section for how each is used. Mirrors
# backends/postgres.py's own constants of the same name/purpose.
MAX_COLUMNS_FOR_SAMPLING = 25
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
# Matches the "GROUP BY ... ORDER BY COUNT(*) DESC TOP 15" shape called for
# in the plan this implements - MSSQL's TOP is used in place of LIMIT (see
# get_schema()'s own frequent-values query below).
FREQUENT_VALUES_LIMIT = 15
# Cardinality gate for "is this categorical column worth sampling frequent
# values for at all" - unlike Postgres (pg_stats.n_distinct, a planner
# statistic, no live scan needed), SQL Server has no equivalently cheap,
# reliably-queryable-the-same-way stat available through this codebase's
# existing conventions here, so this gate is a live `COUNT(DISTINCT col)`
# scan instead - acceptable since this whole section only ever runs in the
# deep/live-query Phase 2 path already, never Phase 1. A column whose
# distinct-count is at least this fraction of the table's live row count is
# treated as "near-unique" and skipped (same "not worth showing frequent
# values for an almost-unique column" reasoning as Postgres's
# _is_near_unique_n_distinct, just computed directly instead of read off a
# stored planner estimate). Capped to at most
# MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE gate queries per table (same cap
# that already bounds the frequent-value queries themselves) rather than
# gating every categorical column in a wide table, since each gate query is
# itself a real scan, not a free lookup.
NEAR_UNIQUE_DISTINCT_RATIO = 0.5


def _quote_ident(name):
    """Bracket-quotes a SQL Server identifier for interpolation into a
    plain SQL string (escaping an embedded ']' by doubling it, the way
    T-SQL itself expects) - used only by Phase 2's per-table/per-column
    live queries below, where a handful of genuinely dynamic statements
    keep the code readable with plain string building rather than a query-
    builder abstraction (mirrors backends/postgres.py's own _quote_ident).
    `name` always comes from INFORMATION_SCHEMA/sys.* data this same
    connection already queried in Phase 1 (kept_names / column names) -
    never raw user input - so this only needs to be correct, not defend
    against adversarial identifiers."""
    return "[" + str(name).replace("]", "]]") + "]"


def _quoted_table_ref(mssql_schema, table_name):
    """[schema].[table] when an explicit schema override is configured,
    else just [table] - the live-query counterpart to get_schema()'s own
    display-only `schema_prefix` (see module docstring for why an
    unqualified reference must be schema-qualified when an override is
    configured: T-SQL has no session-level mechanism to make that
    resolution automatic the way Postgres's search_path/Oracle's ALTER
    SESSION SET CURRENT_SCHEMA do)."""
    if mssql_schema:
        return f"{_quote_ident(mssql_schema)}.{_quote_ident(table_name)}"
    return _quote_ident(table_name)


def _connect_with_hard_timeout(connect_kwargs, timeout_seconds):
    """Runs pytds.connect(**connect_kwargs), bounded by a real external
    deadline - unlike every other network-dialing backend in this app
    (Postgres/MySQL/Redshift/Oracle/Snowflake), passing `login_timeout` to
    pytds.connect() alone is NOT sufficient here, even though pytds's own
    docs describe it the same way those drivers describe their own
    connect_timeout/tcp_connect_timeout/login_timeout kwargs (see
    backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring).

    Confirmed by reading pytds's own installed source (python-tds 1.17.1):
    pytds.utils.exponential_backoff (which pytds.connect()'s retry loop
    calls) tracks elapsed time via a running `cur_time` that gets bumped by
    each attempt's own *allotted* sub-timeout - not by how long that
    attempt actually took - whenever an attempt overruns it (exactly what
    the "Work attempt exceeded it's allocated time" WARNING log lines mean:
    every attempt overran, every time). That accounting quirk lets the
    total wall-clock time before pytds finally gives up run well past the
    `login_timeout` we hand it - observed in production taking ~3x the
    configured budget against a since-unreachable Azure SQL Database
    preset. There is no pytds.connect() kwarg that closes this gap (a
    single retry-disabling flag exists, but the same per-attempt
    accounting quirk still applies to that one remaining attempt if IT
    hangs) - the only reliable fix is exactly this app's other precedent
    for "a third-party client's own timeout can't be trusted as a hard
    deadline": execute_routes.py's _execute_with_timeout, which wraps
    backend.execute() in a ThreadPoolExecutor and enforces
    future.result(timeout=...) itself, external to whatever the driver
    does or doesn't honor internally.

    `timeout_seconds <= 0` disables this entirely (mirroring
    SQL_EXECUTE_TIMEOUT_SECONDS's own escape hatch in execute_routes.py) -
    pytds.connect() then runs exactly as it did before this wrapper
    existed, sole reliance on pytds's own (imperfect) login_timeout
    accounting.

    On a real timeout, the background thread is abandoned (not joined) -
    same "the caller must not block waiting for it" reasoning
    _execute_with_timeout's own docstring gives - but unlike an abandoned
    query, an abandoned connect() that eventually DOES succeed leaves a
    real, live SQL Server connection open with nothing left to ever close
    it. A done-callback added after the timeout closes that connection the
    moment it actually arrives, so a slow-but-eventually-successful connect
    doesn't leak a live server-side session forever."""
    if timeout_seconds <= 0:
        return pytds.connect(**connect_kwargs)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(pytds.connect, **connect_kwargs)
    try:
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            future.add_done_callback(_close_late_connection)
            raise TimeoutError(
                f"Connection to SQL Server timed out after {timeout_seconds:g} seconds"
            ) from None
    finally:
        # wait=False, always - see docstring above: this function must not
        # block on the abandoned connect() attempt once it has already
        # given up (or already returned).
        pool.shutdown(wait=False)


def _close_late_connection(future):
    """Done-callback for a connect() attempt that outlived our own timeout
    (see _connect_with_hard_timeout above) - closes the connection if one
    eventually arrived, so it doesn't stay open on the server forever with
    no reference left anywhere in this process to close it. Silently
    no-ops for the far more common case (the attempt eventually raised too,
    same underlying unreachable host) - nothing to close there."""
    try:
        connection = future.result()
    except Exception:
        return
    try:
        connection.close()
    except Exception:
        pass


def _validate_host_via_cryptography(cert, name):
    """Drop-in replacement for pytds.tls.validate_host - see this module's
    docstring for why pytds's own implementation (which calls pyOpenSSL's
    removed X509.get_extension()) breaks on pyOpenSSL >= 26.2.0. Same
    signature/semantics as the original: True if the certificate's CN or
    any subjectAltName DNS entry matches `name` (accepting a single-label
    "*." wildcard prefix, the same limited form pytds's own version
    supports), False otherwise."""
    host_name = name.decode("ascii") if isinstance(name, bytes) else name
    cc = cert.to_cryptography()

    cn_attrs = cc.subject.get_attributes_for_oid(_NameOID.COMMON_NAME)
    if cn_attrs and cn_attrs[0].value == host_name:
        return True

    try:
        san_ext = cc.extensions.get_extension_for_oid(_ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except _crypto_x509.ExtensionNotFound:
        return False

    for dns_name in san_ext.value.get_values_for_type(_crypto_x509.DNSName):
        if dns_name == host_name:
            return True
        if dns_name[:2] == "*.":
            after_star = dns_name[2:]
            host_after_first_label = ".".join(host_name.split(".")[1:])
            if after_star == host_after_first_label:
                return True
    return False


# Applied at import time, once - every mssql connection with encrypt=true
# (the default) goes through pytds's TLS handshake code, which looks up
# this name as a plain module global at call time (late-bound, not
# pre-imported by reference), so replacing it here takes effect for every
# connection made afterward regardless of when this module happens to be
# imported relative to any given connect() call.
pytds.tls.validate_host = _validate_host_via_cryptography


class MssqlBackend(Backend):
    dialect_name = "Microsoft SQL Server"

    # SQL Server supports a bare "SELECT 1" - the base class's default is
    # correct as-is, no override needed (unlike Oracle's "SELECT 1 FROM
    # DUAL").
    liveness_sql = "SELECT 1"

    def connect(self, descriptor):
        descriptor = descriptor or {}
        host = descriptor.get("host") or ""
        port = descriptor.get("port") or 1433
        database = descriptor.get("database") or ""
        user = descriptor.get("user") or ""
        password = descriptor.get("password") or ""
        schema = descriptor.get("schema") or None
        use_encrypt = descriptor.get("encrypt")
        if use_encrypt is None:
            # Absent (as opposed to explicitly False) defaults to
            # encrypted - see module docstring for why this dialect's
            # default leans the opposite way from Oracle's "ssl" (which
            # defaults off at this layer): a SQL Server deployment that
            # requires encryption (Azure SQL Database always does) simply
            # fails outright with no encryption attempted at all, whereas
            # an Oracle instance without TLS configured works fine either
            # way - so defaulting to on here is the safer failure mode.
            use_encrypt = True

        if not host:
            raise ValueError("SQL Server connection requires a host - none was provided.")
        if not database:
            raise ValueError("SQL Server connection requires a database - none was provided.")
        if not (user and password):
            raise ValueError("SQL Server connection requires a user and password - one was missing.")

        # login_timeout (not the separate "timeout" kwarg, which bounds
        # per-query socket reads) is pytds's connect/login-phase timeout -
        # see backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring for
        # why only the connect phase gets capped, never query execution
        # afterwards (the same principle backends/databricks.py's connect()
        # docstring explains at length). autocommit is a connect-time
        # constructor kwarg for pytds (unlike Oracle's/Redshift's drivers,
        # which only expose it as a settable post-connect property), so
        # there's no separate "connection.autocommit = True" statement
        # needed in execute() below the way there is for those two.
        # resolve_timeout_seconds() lets this preset/custom connection's own
        # "connect_timeout_seconds" override the shared DB_CONNECT_TIMEOUT_SECONDS
        # default - computed once and reused for both the driver's own
        # login_timeout kwarg AND _connect_with_hard_timeout's external
        # deadline below, so the two stay in sync (see that function's own
        # docstring for why pytds needs both).
        connect_timeout = resolve_timeout_seconds(
            descriptor, "connect_timeout_seconds", DB_CONNECT_TIMEOUT_SECONDS,
        )
        kwargs = {
            "server": host, "port": port, "database": database,
            "user": user, "password": password,
            "autocommit": True,
            "login_timeout": connect_timeout,
        }
        if use_encrypt:
            # See module docstring: pytds only attempts TLS at all when
            # handed a CA bundle to validate the server's certificate
            # against - certifi's bundled public CA list covers the
            # realistic common case (Azure SQL Database, or any on-prem
            # box with a certificate chaining to a public root).
            kwargs["cafile"] = certifi.where()

        connection = _connect_with_hard_timeout(kwargs, connect_timeout)
        # Stashed on the connection itself, not threaded through as a
        # get_schema() parameter - the Backend ABC's get_schema(connection)
        # signature (shared by all 8 dialects) takes only a connection, not
        # the original descriptor, and unlike Oracle's/Redshift's connect()
        # (which bake a schema override into the session itself via
        # ALTER SESSION/SET search_path, so get_schema() can just ask the
        # session what its own current schema is), this dialect has no
        # session-level equivalent to bake it into (see module docstring) -
        # so get_schema() below reads it back off the connection object
        # instead. None when the descriptor didn't specify one, which
        # get_schema()'s COALESCE(%s, SCHEMA_NAME()) scoping treats
        # correctly as "no override, use the login's own default schema."
        connection.mssql_schema = schema
        return connection

    def close(self, connection):
        if connection:
            connection.close()

    def cache_key(self, descriptor):
        """host:port/database.schema, parsed straight from the descriptor
        - never a credential. Same non-sensitive-identifier role
        RedshiftBackend's/OracleBackend's cache_key plays."""
        descriptor = descriptor or {}
        host = descriptor.get("host") or "unknown"
        port = descriptor.get("port") or "unknown"
        database = descriptor.get("database") or "unknown"
        schema = descriptor.get("schema") or "dbo"
        return f"{host}:{port}/{database}.{schema}"

    def identity_label(self, connection):
        db_name, username = "Unknown", "Unknown"
        with connection.cursor() as cursor:
            cursor.execute("SELECT DB_NAME(), SYSTEM_USER;")
            row = cursor.fetchone()
            if row:
                db_name, username = row[0], row[1]
        return db_name, username

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - see backends/base.py's
        Backend.get_schema()/get_schema_shallow() docstrings and
        backends/postgres.py's own _build_shallow_schema_parts() for the
        worked example this mirrors.

        Returns None if the connection's (resolved) schema has no BASE
        TABLE at all (mirrors get_schema()'s old "return None" for that
        case). Otherwise returns (schema_parts, table_columns, phase2_ctx):
          - schema_parts: the ordered list of text sections, not yet
            joined/capped.
          - table_columns: {table_name: [column_name, ...]}, scoped to the
            same bounded kept_names set - handed to the shared
            find_naming_convention_relationships() helper by get_schema()'s
            Phase 2 pass (no extra query needed for that).
          - phase2_ctx: raw, already-fetched data Phase 2 wants to reuse
            without re-querying - kept_names/column_types (for deciding
            what to sample), schema_prefix (for qualifying Phase 2's own
            rendered lines the same way Phase 1's headings are qualified),
            and the raw views/routines rows (so get_schema() can render
            their full body text without a second trip to the database -
            see the "Views"/"Routines" sections below for why only the
            *name*/*signature* is rendered here).
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
            # view/etc. query runs, same staging as every other backend's
            # schema introspection. Scoped by COALESCE(%s, SCHEMA_NAME())
            # rather than a hardcoded 'dbo' - see module docstring for why
            # there's no session-level schema override to rely on instead.
            # T-SQL's TOP (not LIMIT) caps the row count - TOP (%s) accepts
            # a bound parameter here the same way LIMIT %s does for
            # Postgres/MySQL.
            cursor.execute("""
                SELECT TOP (%s) TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = COALESCE(%s, SCHEMA_NAME())
                  AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME;
            """, (SCHEMA_MAX_TABLE_NAMES_SCANNED, connection.mssql_schema))
            all_table_names = [row[0] for row in cursor.fetchall()]

            if not all_table_names:
                return None

            # Every table/view name rendered below gets this prefix when an
            # explicit "schema" override was configured - see the module
            # docstring for why: unqualified names generated from this
            # schema text would otherwise silently resolve into the
            # connecting login's own default schema at execution time, not
            # this override, since T-SQL has no session-level mechanism (the
            # way Postgres's search_path/Oracle's ALTER SESSION SET
            # CURRENT_SCHEMA have) to make that resolution automatic. Left
            # empty when no override is configured - an unqualified
            # reference already resolves correctly in that case, into the
            # same default schema this introspection itself just queried
            # via SCHEMA_NAME(), so qualifying would add nothing but noise.
            schema_prefix = f"{connection.mssql_schema}." if connection.mssql_schema else ""

            kept_names, shard_groups = group_date_sharded_tables(all_table_names)
            kept_names, shard_groups, omitted_count = cap_kept_tables(kept_names, shard_groups)
            # No native wildcard-table query mechanism (unlike BigQuery), so
            # a shard family's representative is described under its own
            # real, literal name - mirrors every other backend here.
            shard_by_representative = {
                members[-1]: (prefix, members) for prefix, members in shard_groups.items()
            }

            # 1. Tables and columns - scoped to the bounded kept_names set.
            # IN-clause built the same way backends/mysql.py's is (pytds's
            # paramstyle is "pyformat", same %s-per-item substitution
            # PyMySQL uses - see module docstring), not Oracle's named
            # :name binding. Column shape is unchanged from before this
            # two-phase refactor (no new columns joined into this SELECT) -
            # the new identity marker below comes from a separate
            # sys.identity_columns query instead (query #5 below), so no
            # existing test's columns_rows tuples need re-padding the way
            # backends/postgres.py's own is_identity/identity_generation
            # columns needed.
            format_strings = ",".join(["%s"] * len(kept_names))
            cursor.execute(f"""
                SELECT
                    c.TABLE_NAME,
                    c.COLUMN_NAME,
                    c.DATA_TYPE,
                    c.IS_NULLABLE,
                    c.COLUMN_DEFAULT
                FROM INFORMATION_SCHEMA.COLUMNS c
                WHERE c.TABLE_SCHEMA = COALESCE(%s, SCHEMA_NAME())
                  AND c.TABLE_NAME IN ({format_strings})
                ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION;
            """, (connection.mssql_schema,) + tuple(kept_names))
            columns_data = cursor.fetchall()

            for table_name, col_name, data_type, is_nullable, col_default in columns_data:
                table_columns.setdefault(table_name, []).append(col_name)
                column_types.setdefault(table_name, {})[col_name] = data_type

            # 2. Constraints - unlike Redshift/Snowflake/Databricks, SQL
            # Server DOES enforce PK/FK/UNIQUE at write time, so the
            # wording here says so rather than reusing their "declared
            # only" caption. FK targets are resolved via
            # REFERENTIAL_CONSTRAINTS + a second KEY_COLUMN_USAGE join
            # against its UNIQUE_CONSTRAINT_NAME - the standard pattern for
            # SQL Server's ANSI-compliant information_schema (which, unlike
            # Postgres, has no single constraint_column_usage view that
            # already carries the FK target directly). Best-effort:
            # wrapped in try/except like every other backend's constraints
            # query, in case a role lacks catalog visibility.
            constraints = []
            try:
                cursor.execute(f"""
                    SELECT
                        tc.TABLE_NAME,
                        tc.CONSTRAINT_NAME,
                        tc.CONSTRAINT_TYPE,
                        kcu.COLUMN_NAME,
                        ccu.TABLE_NAME AS FOREIGN_TABLE_NAME,
                        ccu.COLUMN_NAME AS FOREIGN_COLUMN_NAME
                    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                    LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                      ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                     AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                    LEFT JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                      ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                     AND tc.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
                    LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ccu
                      ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME
                     AND rc.UNIQUE_CONSTRAINT_SCHEMA = ccu.TABLE_SCHEMA
                    WHERE tc.TABLE_SCHEMA = COALESCE(%s, SCHEMA_NAME())
                      AND tc.TABLE_NAME IN ({format_strings})
                    ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME;
                """, (connection.mssql_schema,) + tuple(kept_names))
                constraints = cursor.fetchall()
            except Exception:
                pass

            # 3. Views - deliberately NOT scoped to kept_names, same
            # reasoning as every other backend here: that set is built
            # exclusively from BASE TABLE names, so no view name could ever
            # appear in it. Best-effort, same as the constraints query.
            #
            # Shallow rendering is name-only (no VIEW_DEFINITION body) - the
            # raw rows (including each view's body) are still fetched here
            # (one query, reused by both phases) and threaded through via
            # phase2_ctx below so get_schema() (deep) can render the full
            # body without a second query - see get_schema()'s "View
            # definitions" section. VIEW_DEFINITION can come back NULL for a
            # view created WITH ENCRYPTION - existing behavior (the `(v[1]
            # or '').strip()` guard), kept as-is, not new to this refactor.
            views = []
            try:
                cursor.execute("""
                    SELECT TABLE_NAME, VIEW_DEFINITION
                    FROM INFORMATION_SCHEMA.VIEWS
                    WHERE TABLE_SCHEMA = COALESCE(%s, SCHEMA_NAME());
                """, (connection.mssql_schema,))
                views = cursor.fetchall()
                if views:
                    view_lines = [f"  View {schema_prefix}{v[0]}" for v in views]
                    schema_parts.append("Views:\n" + "\n".join(view_lines))
            except Exception:
                pass

            # 4. Identity columns (new) - sys.identity_columns is SQL
            # Server's own catalog view listing every IDENTITY column
            # directly (object_id + column name), cheaper and simpler to
            # join here than repeatedly calling the scalar
            # COLUMNPROPERTY(object_id(...), col, 'IsIdentity') function
            # once per column row. Joined through sys.tables/sys.schemas
            # (not INFORMATION_SCHEMA, which has no identity-column view at
            # all) the same way every other sys.* section below is.
            identity_columns = set()
            try:
                cursor.execute(f"""
                    SELECT t.name, ic.name
                    FROM sys.identity_columns ic
                    JOIN sys.tables t ON ic.object_id = t.object_id
                    JOIN sys.schemas s ON t.schema_id = s.schema_id
                    WHERE s.name = COALESCE(%s, SCHEMA_NAME())
                      AND t.name IN ({format_strings});
                """, (connection.mssql_schema,) + tuple(kept_names))
                identity_columns = {(tbl, col) for tbl, col in cursor.fetchall()}
            except Exception:
                pass

            # Now that identity_columns is known, build each table's
            # rendered column lines (IDENTITY marker included) and the
            # per-table/-family headings - deferred to here (rather than
            # done inline while columns_data was first read above) purely
            # so the marker can be applied in one pass; the query order
            # above is unaffected either way.
            tables = {}
            for table_name, col_name, data_type, is_nullable, col_default in columns_data:
                default_str = f" DEFAULT {col_default}" if col_default is not None else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                identity_str = " IDENTITY" if (table_name, col_name) in identity_columns else ""
                tables.setdefault(table_name, []).append(
                    f"  {col_name} {data_type} {null_str}{default_str}{identity_str}"
                )

            table_section_parts = []
            for table_name in kept_names:
                col_defs = tables.get(table_name)
                if not col_defs:
                    continue
                if table_name in shard_by_representative:
                    prefix, members = shard_by_representative[table_name]
                    heading = (
                        f"Table family: {schema_prefix}{prefix}_<date> ({len(members)} date-sharded tables, "
                        f"e.g. {schema_prefix}{members[0]} .. {schema_prefix}{members[-1]}; identical columns in every "
                        f"member - substitute the exact table name for whichever date is "
                        f"meant, following this same naming pattern; never query "
                        f"'{schema_prefix}{prefix}_<date>' literally)"
                    )
                else:
                    heading = f"Table: {schema_prefix}{table_name}"
                table_section_parts.append(heading + "\n" + "\n".join(col_defs))

            # Inserted ahead of Constraints/Views (which were fetched above
            # but not yet appended) so the final section order matches the
            # original file's presentation: Tables, then Constraints, then
            # Views, then the new Phase 1 sections.
            schema_parts = table_section_parts + schema_parts

            if omitted_count:
                schema_parts.append(
                    f"[... {omitted_count} more table(s)/table-family(ies) not shown - "
                    f"this schema has more than the {SCHEMA_MAX_TABLES}-table summary "
                    f"limit. Ask about a narrower set of tables to see the rest.]"
                )

            if constraints:
                lines = []
                for tbl, c_name, c_type, col, f_tbl, f_col in constraints:
                    if c_type == 'FOREIGN KEY' and f_tbl:
                        lines.append(f"  [{schema_prefix}{tbl}] {c_name} ({c_type}): {col} -> {schema_prefix}{f_tbl}({f_col})")
                    elif col:
                        lines.append(f"  [{schema_prefix}{tbl}] {c_name} ({c_type}): {col}")
                    else:
                        lines.append(f"  [{schema_prefix}{tbl}] {c_name} ({c_type})")
                schema_parts.append(
                    "Constraints (enforced at write time):\n" + "\n".join(lines)
                )

            # 5. Comments (new) - sys.extended_properties is SQL Server's
            # own generic metadata-annotation mechanism; 'MS_Description'
            # is the convention SSMS and most SQL Server tooling use for
            # table/column descriptions (there's no simpler ANSI
            # equivalent the way Postgres has obj_description()/
            # col_description() - this is the closest analogue). class = 1
            # is MSSQL's own constant for "object or column" extended
            # properties (as opposed to e.g. class 0 = database-level).
            # minor_id = 0 means a table-level property (no matching
            # sys.columns row, so the LEFT JOIN naturally leaves
            # column_name NULL); minor_id = a real column_id means a
            # column-level one. Best-effort/try-except, like every new
            # optional section below - a role that can't see extended
            # properties still gets every other section.
            try:
                cursor.execute(f"""
                    SELECT t.name, c.name, CAST(ep.value AS NVARCHAR(MAX))
                    FROM sys.extended_properties ep
                    JOIN sys.tables t ON ep.major_id = t.object_id
                    JOIN sys.schemas s ON t.schema_id = s.schema_id
                    LEFT JOIN sys.columns c
                      ON ep.minor_id = c.column_id AND c.object_id = t.object_id
                    WHERE ep.class = 1
                      AND ep.name = 'MS_Description'
                      AND s.name = COALESCE(%s, SCHEMA_NAME())
                      AND t.name IN ({format_strings});
                """, (connection.mssql_schema,) + tuple(kept_names))
                comment_lines = []
                for tbl, col, comment in cursor.fetchall():
                    if not comment:
                        continue
                    if col:
                        comment_lines.append(f"  [column] {schema_prefix}{tbl}.{col}: {comment}")
                    else:
                        comment_lines.append(f"  [table] {schema_prefix}{tbl}: {comment}")
                if comment_lines:
                    schema_parts.append("Comments:\n" + "\n".join(comment_lines))
            except Exception:
                pass

            # 6. Row count estimate (new) - sys.dm_db_partition_stats is a
            # free catalog/DMV lookup (not a live scan): summing row_count
            # across index_id 0 (heap) and 1 (clustered index) per table
            # gives the table's own current row-count bookkeeping, the
            # closest SQL Server equivalent to Postgres's pg_class.reltuples
            # or MySQL's TABLES.TABLE_ROWS - see get_schema()'s "Live row
            # counts" section for the authoritative, deep-only counterpart.
            try:
                cursor.execute(f"""
                    SELECT t.name, SUM(ps.row_count)
                    FROM sys.dm_db_partition_stats ps
                    JOIN sys.tables t ON ps.object_id = t.object_id
                    JOIN sys.schemas s ON t.schema_id = s.schema_id
                    WHERE ps.index_id IN (0, 1)
                      AND s.name = COALESCE(%s, SCHEMA_NAME())
                      AND t.name IN ({format_strings})
                    GROUP BY t.name;
                """, (connection.mssql_schema,) + tuple(kept_names))
                estimate_lines = []
                for tbl, row_count in cursor.fetchall():
                    if row_count is None:
                        continue
                    estimate_lines.append(f"  {schema_prefix}{tbl}: ~{int(row_count)} rows (estimate)")
                if estimate_lines:
                    schema_parts.append("Row count estimates:\n" + "\n".join(estimate_lines))
            except Exception:
                pass

            # 7. Routines (new) - existence + signature only, no body (see
            # get_schema()'s "Routine definitions" section for the full-body
            # deep-only counterpart, reusing sys.sql_modules.definition
            # fetched here rather than re-querying it). type IN ('P', 'FN',
            # 'TF', 'IF') covers stored procedures, scalar functions,
            # (multi-statement and inline) table-valued functions - the
            # practical "routine" set a generated query could plausibly
            # reference or need to know about, mirroring what
            # information_schema.ROUTINES would report for Postgres/MySQL.
            # One row per parameter (parameter_id > 0 excludes a scalar
            # function's own "parameter 0" return-value placeholder row) -
            # aggregated into one routine per name in Python below rather
            # than via STRING_AGG (added only in SQL Server 2017+ - a flat
            # per-parameter row shape here works on older versions too, the
            # same "don't assume a version-specific feature" caution this
            # file's docstring already applies elsewhere). Not scoped to
            # kept_names (like Views above) - routines aren't tables, and
            # COALESCE(%s, SCHEMA_NAME()) alone already bounds this to the
            # connection's own schema.
            routines = {}
            try:
                cursor.execute("""
                    SELECT o.name, o.type_desc, p.name, p.parameter_id,
                           TYPE_NAME(p.user_type_id), sm.definition
                    FROM sys.objects o
                    JOIN sys.schemas s ON o.schema_id = s.schema_id
                    LEFT JOIN sys.parameters p
                      ON p.object_id = o.object_id AND p.parameter_id > 0
                    LEFT JOIN sys.sql_modules sm ON sm.object_id = o.object_id
                    WHERE o.type IN ('P', 'FN', 'TF', 'IF')
                      AND s.name = COALESCE(%s, SCHEMA_NAME())
                    ORDER BY o.name, p.parameter_id;
                """, (connection.mssql_schema,))
                for name, type_desc, param_name, _param_id, param_type, definition in cursor.fetchall():
                    entry = routines.setdefault(name, {"type_desc": type_desc, "params": [], "definition": definition})
                    if definition is not None:
                        entry["definition"] = definition
                    if param_name:
                        entry["params"].append(f"{param_name} {param_type}" if param_type else param_name)
                if routines:
                    routine_lines = [
                        f"  {name}({', '.join(r['params'])}) [{r['type_desc']}]"
                        for name, r in routines.items()
                    ]
                    schema_parts.append("Routines:\n" + "\n".join(routine_lines))
            except Exception:
                pass

            # 8. Session facts (new) - one line for the whole connection,
            # not per-table. Unlike Postgres's current_setting('TimeZone')/
            # MySQL's @@session.time_zone, SQL Server has no real per-
            # session timezone concept the way those dialects do (DATETIME/
            # DATETIME2 arithmetic is timezone-naive; only DATETIMEOFFSET
            # carries an explicit UTC offset per value, not a session-wide
            # default) - so only the server's default collation is reported
            # here, never a fabricated timezone value.
            try:
                cursor.execute("SELECT CAST(SERVERPROPERTY('Collation') AS NVARCHAR(128));")
                row = cursor.fetchone()
                if row and row[0]:
                    schema_parts.append(f"Session: default collation={row[0]}")
            except Exception:
                pass

            # 9. Grants (new, no longer deferred) - see module docstring
            # for why this one (unlike Indexes/Triggers, still deferred)
            # is being added now. INFORMATION_SCHEMA.TABLE_PRIVILEGES is
            # SQL Server's own ANSI-standard grants view (there's no
            # role_table_grants here, same gap MySQL has - see
            # backends/mysql.py's own Grants section, which this mirrors
            # almost verbatim) - it already only reports grants visible to
            # the connecting login, so this is inherently current-user-
            # scoped without any extra WHERE clause needed.
            try:
                cursor.execute(f"""
                    SELECT GRANTEE, TABLE_NAME, PRIVILEGE_TYPE
                    FROM INFORMATION_SCHEMA.TABLE_PRIVILEGES
                    WHERE TABLE_SCHEMA = COALESCE(%s, SCHEMA_NAME())
                      AND TABLE_NAME IN ({format_strings})
                    ORDER BY TABLE_NAME, GRANTEE;
                """, (connection.mssql_schema,) + tuple(kept_names))
                grants = cursor.fetchall()
                if grants:
                    grant_lines = [f"  Grant {g[2]} on {schema_prefix}{g[1]} to {g[0]}" for g in grants]
                    schema_parts.append("Grants:\n" + "\n".join(grant_lines))
            except Exception:
                pass

            # 10. RLS / external-table flags (new) - sys.security_policies
            # (SQL Server 2016+) existence check via sys.security_predicates
            # for row-level security; sys.external_tables (PolyBase,
            # 2016+) for an external/federated table flag. Both are
            # version-/edition-dependent catalog views that may not exist
            # at all on an older SQL Server - wrapped in try/except so a
            # permissions gap OR a missing catalog view on either one
            # degrades gracefully rather than losing the whole schema
            # fetch. Deliberately silent (no line at all) for a table where
            # neither flag is true, per the plan's "don't render anything...
            # to avoid noise" instruction - same posture as
            # backends/postgres.py's own RLS/federation section.
            rls_tables = set()
            try:
                cursor.execute(f"""
                    SELECT DISTINCT t.name
                    FROM sys.security_predicates sp
                    JOIN sys.security_policies pol
                      ON sp.object_id = pol.object_id AND pol.is_enabled = 1
                    JOIN sys.tables t ON sp.target_object_id = t.object_id
                    JOIN sys.schemas s ON t.schema_id = s.schema_id
                    WHERE s.name = COALESCE(%s, SCHEMA_NAME())
                      AND t.name IN ({format_strings});
                """, (connection.mssql_schema,) + tuple(kept_names))
                rls_tables = {row[0] for row in cursor.fetchall()}
            except Exception:
                pass

            external_tables = set()
            try:
                cursor.execute(f"""
                    SELECT t.name
                    FROM sys.external_tables t
                    JOIN sys.schemas s ON t.schema_id = s.schema_id
                    WHERE s.name = COALESCE(%s, SCHEMA_NAME())
                      AND t.name IN ({format_strings});
                """, (connection.mssql_schema,) + tuple(kept_names))
                external_tables = {row[0] for row in cursor.fetchall()}
            except Exception:
                pass

            if rls_tables or external_tables:
                flag_lines = []
                for table_name in kept_names:
                    annotations = []
                    if table_name in rls_tables:
                        annotations.append("[RLS enabled]")
                    if table_name in external_tables:
                        annotations.append("[external table]")
                    if annotations:
                        flag_lines.append(f"  {schema_prefix}{table_name}: {' '.join(annotations)}")
                if flag_lines:
                    schema_parts.append(
                        "Row-level security / external tables:\n" + "\n".join(flag_lines)
                    )

        phase2_ctx = {
            "kept_names": kept_names,
            "column_types": column_types,
            "schema_prefix": schema_prefix,
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
        schema_prefix = phase2_ctx["schema_prefix"]
        views = phase2_ctx["views"]
        routines = phase2_ctx["routines"]

        # Phase 2 (deep-only): full view/routine bodies, reusing the raw
        # rows _build_shallow_schema_parts already fetched - no re-query.
        # VIEW_DEFINITION/sys.sql_modules.definition can legitimately come
        # back NULL for an object created WITH ENCRYPTION - the `(... or
        # '').strip()` guard below (existing behavior, not new) keeps that
        # from raising and simply omits that one entry rather than aborting
        # the whole schema fetch.
        if views:
            view_lines = [f"  View {schema_prefix}{v[0]}: {(v[1] or '').strip()}" for v in views]
            schema_parts.append("View definitions:\n" + "\n".join(view_lines))

        routine_body_lines = [
            f"  {name}: {(r['definition'] or '').strip()}"
            for name, r in routines.items() if (r['definition'] or '').strip()
        ]
        if routine_body_lines:
            schema_parts.append("Routine definitions:\n" + "\n".join(routine_body_lines))

        with connection.cursor() as cursor:
            # Per-table Phase 2 live queries: fresh row count (authoritative,
            # vs. the Phase 1 dm_db_partition_stats estimate above, which is
            # free but can lag until the next stats update), then bounded
            # column sampling. Cardinality gating for "is this categorical
            # column worth sampling" uses a live COUNT(DISTINCT col) scan
            # (see NEAR_UNIQUE_DISTINCT_RATIO's docstring above for why -
            # SQL Server has no Postgres-pg_stats-style free planner
            # estimate reliably queryable the same way here), capped to at
            # most MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE gate queries per
            # table (same cap that already bounds the frequent-value queries
            # themselves), so a table with many text columns can cost at
            # most 1 (live count) + 1 (combined min/max) +
            # 2 * MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE (gate + frequent-
            # values, per eligible categorical column) queries - bounded and
            # documented, not unbounded per-column fan-out.
            live_count_lines = []
            sample_blocks = []
            row_counts_by_table = {}
            for table_name in kept_names:
                col_types = column_types.get(table_name) or {}
                if not col_types:
                    # A kept_names entry cap_kept_tables dropped columns for
                    # (shouldn't normally happen - kept_names and
                    # column_types are built from the same query - but
                    # guards against an empty/omitted table cleanly).
                    continue

                table_ref = _quoted_table_ref(connection.mssql_schema, table_name)

                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {table_ref};")
                    row = cursor.fetchone()
                    if row is not None:
                        row_counts_by_table[table_name] = row[0]
                        live_count_lines.append(f"  {schema_prefix}{table_name}: {row[0]} rows (live, authoritative)")
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
                ][:MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE]

                table_sample_lines = []

                # Min/max, all eligible numeric/date columns in one combined
                # query per table (bounded by MAX_NUMERIC_COLUMNS_FOR_MINMAX)
                # rather than one query per column.
                if numeric_cols:
                    try:
                        select_parts = ", ".join(
                            f"MIN({_quote_ident(c)}) AS {_quote_ident(c + '__min')}, "
                            f"MAX({_quote_ident(c)}) AS {_quote_ident(c + '__max')}"
                            for c in numeric_cols
                        )
                        cursor.execute(f"SELECT {select_parts} FROM {table_ref};")
                        row = cursor.fetchone()
                        if row is not None:
                            for i, c in enumerate(numeric_cols):
                                min_v, max_v = row[2 * i], row[2 * i + 1]
                                table_sample_lines.append(f"    {c}: range [{min_v} .. {max_v}]")
                    except Exception:
                        pass

                # Frequent values - one gate query (COUNT(DISTINCT ...)) plus
                # one GROUP BY query per eligible categorical column (both
                # capped at MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE per
                # table, see this method's docstring above). A column whose
                # distinct-count is at least NEAR_UNIQUE_DISTINCT_RATIO of
                # the table's own live row count is skipped as near-unique -
                # permissive (proceeds to sample) whenever the gate query
                # itself fails or the live count is unknown/zero, same
                # "don't silently hide sampling" posture
                # backends/postgres.py's own gate takes for an unanalyzed
                # column.
                total_rows = row_counts_by_table.get(table_name)
                for c in categorical_cols:
                    distinct_count = None
                    try:
                        cursor.execute(f"SELECT COUNT(DISTINCT {_quote_ident(c)}) FROM {table_ref};")
                        gate_row = cursor.fetchone()
                        if gate_row is not None:
                            distinct_count = gate_row[0]
                    except Exception:
                        pass

                    if (
                        distinct_count is not None and total_rows
                        and (distinct_count / total_rows) >= NEAR_UNIQUE_DISTINCT_RATIO
                    ):
                        continue

                    try:
                        cursor.execute(
                            f"SELECT TOP {FREQUENT_VALUES_LIMIT} {_quote_ident(c)}, COUNT(*) "
                            f"FROM {table_ref} GROUP BY {_quote_ident(c)} ORDER BY COUNT(*) DESC;"
                        )
                        freq_rows = cursor.fetchall()
                        if freq_rows:
                            freq_text = ", ".join(f"{val} ({cnt})" for val, cnt in freq_rows)
                            table_sample_lines.append(f"    {c}: frequent values = {freq_text}")
                    except Exception:
                        pass

                if table_sample_lines:
                    sample_blocks.append(f"  Table: {schema_prefix}{table_name}\n" + "\n".join(table_sample_lines))

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
        # No autocommit assignment here (unlike Oracle's/Redshift's
        # execute()) - pytds's autocommit is a connect-time constructor
        # kwarg, already set to True in connect() above.
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