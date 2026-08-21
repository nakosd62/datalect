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
- Indexes/Triggers/Grants sections are deferred from get_schema() below -
  SQL Server supports all three (sys.indexes, sys.triggers,
  information_schema.role_table_grants) and they could be added later, but
  every dialect that could support "nice to have" introspection extras
  deferred at least one of them in its own first pass too (Oracle deferred
  Grants; Redshift deferred Indexes/Triggers/Grants) - this isn't a new
  gap, it's the same "ship core Tables/Columns/Constraints/Views, defer
  the rest" precedent.

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

import certifi
import pytds
import pytds.tls
import sqlparse
from cryptography import x509 as _crypto_x509
from cryptography.x509.oid import ExtensionOID as _ExtensionOID, NameOID as _NameOID

from .base import (
    Backend, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
)


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
        kwargs = {
            "server": host, "port": port, "database": database,
            "user": user, "password": password,
            "autocommit": True,
            "login_timeout": DB_CONNECT_TIMEOUT_SECONDS,
        }
        if use_encrypt:
            # See module docstring: pytds only attempts TLS at all when
            # handed a CA bundle to validate the server's certificate
            # against - certifi's bundled public CA list covers the
            # realistic common case (Azure SQL Database, or any on-prem
            # box with a certificate chaining to a public root).
            kwargs["cafile"] = certifi.where()

        connection = pytds.connect(**kwargs)
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

    def get_schema(self, connection):
        schema_parts = []

        with connection.cursor() as cursor:
            # Phase 1: cheap - just the distinct base-table names, bounded
            # so a schema with an extreme number of tables can't make even
            # this scan unbounded (SCHEMA_MAX_TABLE_NAMES_SCANNED). Grouped
            # into date-shard families and capped to SCHEMA_MAX_TABLES
            # entries (see backends/base.py) before any column/constraint/
            # view query runs, same staging as every other backend's
            # get_schema(). Scoped by COALESCE(%s, SCHEMA_NAME()) rather
            # than a hardcoded 'dbo' - see module docstring for why there's
            # no session-level schema override to rely on instead. T-SQL's
            # TOP (not LIMIT) caps the row count - TOP (%s) accepts a bound
            # parameter here the same way LIMIT %s does for Postgres/MySQL.
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
            # :name binding.
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

            tables = {}
            for table_name, col_name, data_type, is_nullable, col_default in columns_data:
                default_str = f" DEFAULT {col_default}" if col_default is not None else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                tables.setdefault(table_name, []).append(
                    f"  {col_name} {data_type} {null_str}{default_str}"
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
                if constraints:
                    lines = []
                    for tbl, c_name, c_type, col, f_tbl, f_col in constraints:
                        if c_type == 'FOREIGN KEY' and f_tbl:
                            lines.append(f"  [{tbl}] {c_name} ({c_type}): {col} -> {f_tbl}({f_col})")
                        elif col:
                            lines.append(f"  [{tbl}] {c_name} ({c_type}): {col}")
                        else:
                            lines.append(f"  [{tbl}] {c_name} ({c_type})")
                    schema_parts.append(
                        "Constraints (enforced at write time):\n" + "\n".join(lines)
                    )
            except Exception:
                pass

            # 3. Views - deliberately NOT scoped to kept_names, same
            # reasoning as every other backend here: that set is built
            # exclusively from BASE TABLE names, so no view name could ever
            # appear in it. Best-effort, same as the constraints query.
            try:
                cursor.execute("""
                    SELECT TABLE_NAME, VIEW_DEFINITION
                    FROM INFORMATION_SCHEMA.VIEWS
                    WHERE TABLE_SCHEMA = COALESCE(%s, SCHEMA_NAME());
                """, (connection.mssql_schema,))
                views = cursor.fetchall()
                if views:
                    view_lines = [f"  View {v[0]}: {(v[1] or '').strip()}" for v in views]
                    schema_parts.append("Views:\n" + "\n".join(view_lines))
            except Exception:
                pass

            # Deliberately no Indexes/Triggers/Grants sections - see module
            # docstring for why (deferred as follow-up, same as Oracle's/
            # Redshift's own first passes deferring at least one of these).

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
