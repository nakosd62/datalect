"""
backends/mongodb_sql.py

MongoSqlBackend: talks to a MongoDB Atlas SQL Interface endpoint via pyodbc.
Unlike every other backend in this package, MongoDB has no native textual
query language at all - its real interface is MQL (BSON filter documents /
aggregation pipelines), not SQL. What this backend actually talks to is
"MongoSQL", MongoDB's own SQL-92-compatible dialect exposed specifically for
BI-tool interop (Atlas SQL Interface / formerly "BI Connector"), reachable
only via ODBC or JDBC over a live Atlas cluster (5.0+), Atlas Data
Federation, or a self-managed Enterprise Advanced cluster (6.0+) - never
against a plain community-edition deployment. See this app's own README/
help.html for the user-facing explanation of that constraint; this module
just assumes it's already been satisfied (the descriptor's "url" is a
working, already-enabled Atlas SQL connection string) by the time connect()
is called.

Read-only, deliberately: the SQL Interface itself only supports SELECT (see
execute() below, which enforces this defensively even though the driver
would likely reject a write anyway) - there is no MongoSQL equivalent of
INSERT/UPDATE/DELETE the way every other dialect in this app has. This is a
real, permanent capability gap versus every other backend here, not a
temporary limitation - see translate_routes.py's dialect intro for this
backend, which tells Gemini never to attempt a write in the first place.

Connection descriptor shape (see app_config.py's DATABASE_PRESETS_FILE
comment and config_routes.py's custom-connection handling) is FOUR
separate fields, not one packed string:

  {"url": "mongodb://atlas-sql-xxxxx.a.query.mongodb.net/?ssl=true&authSource=admin",
   "database": "mydb", "user": "myuser", "password": "mypassword"}

"url" is a real, bare mongodb:// deployment URI on its own - nothing else
folded into it (see cache_key()'s docstring for why MongoDB counts as a
"real url" dialect, like Postgres/MySQL, unlike Oracle/Redshift/SQL
Server/etc., even though it ALSO carries these extra structured fields
the way those do). "database"/"user"/"password" are ordinary fields, same
shape as Redshift's/SQL Server's own database/user/password - see
config_routes.py's module docstring for the full descriptor-shape
picture across every dialect. Get the exact URI/Database/User/Password
values from Atlas's own "Connect using the Atlas SQL Interface" modal
rather than hand-assembling them - including "authSource=admin" in the
URI's query string: Atlas database users are always defined (and thus
authenticated) against the "admin" database regardless of which specific
database(s) they're granted roles on, and the standard MongoDB
connection-string spec only defaults authSource to "admin" when the URI's
own path has no database segment in it (which is the case here - see
https://www.mongodb.com/docs/manual/reference/connection-string-options/#mongodb-urioption-urioption.authSource)
- explicit is safer than relying on that default, especially since this
driver has already surfaced one documented-vs-actual behavior mismatch
(the UnicodeTranslationOption finding below).

This four-field shape is newer than the ODBC driver pyodbc.connect()
actually needs, which is still a single DSN-less connection string
(semicolon-separated Key=Value pairs: Driver, Uri, Database, User,
Password - see MongoDB's own driver-setup docs,
https://www.mongodb.com/docs/sql-interface/install-driver/). connect()
below reassembles the four descriptor fields into that one string via
_packed_url() + _build_connection_string() - "Driver={...}" (an OS-level
detail of *this server*, see the Dockerfile's odbcinst.ini registration,
which _DRIVER_CLAUSE below must match verbatim) and the "Uri=" key name
itself are both injected there too, never something a user has to type -
see _build_connection_string()'s and _normalize_url()'s own docstrings.
None of this is a user's/preset author's concern; it exists purely so
this module can still hand pyodbc.connect() the single string it expects.

Backward compatible with how this dialect's descriptor used to look,
before it had separate database/user/password fields at all: an older
saved custom connection or preset whose "url" alone still carries
everything packed in (with or without its own "Driver="/"Uri=" prefixes)
continues to work unchanged - _packed_url() below only reassembles the
four fields when "database"/"user"/"password" are actually present on the
descriptor; otherwise "url" is passed through exactly as connect() always
handled it. See _packed_url()'s docstring for the exact detection rule.

CONFIRMED VIA LIVE TESTING - deliberately do NOT add "UnicodeTranslationOption"
(or anything else from the odbc.ini DSN-FILE field table beyond
Driver/Uri/Database/User/Password) to this inline string. MongoDB's docs
show UnicodeTranslationOption as a field in the odbc.ini DSN-file form, but
the real driver rejects it in the DSN-less inline form with
"[MongoDB][Core] Invalid Uri: 'UnicodeTranslationOption' is not a valid URI
keyword (0) (SQLDriverConnect)" - i.e. the odbc.ini DSN-file field set and
the DSN-less inline connection-string keyword set aren't identical, even
though the docs don't call that out. Stick to the fields above unless a
future live test confirms another one actually works inline.

webClient/client.js's UI renders "url"/"database"/"user"/"password" as
four ordinary labeled fields for this dialect (gated on an `isMongoSql`
check) - same generic password-field masking every other credentialed
structured dialect (Oracle/Redshift/SQL Server/...) already gets, no
bespoke ODBC-string mask/unmask helper needed anymore.

This module has now been exercised against a live Atlas SQL cluster (see
the UnicodeTranslationOption finding above) - connect()/execute() basics
are confirmed working. get_schema()'s exact catalog-column assumptions and
liveness_sql's bare "SELECT 1" are still the two things least proven live,
since they weren't part of the first smoke test.
"""

import re

import pyodbc
import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS, cap_kept_tables, cap_schema_text,
)

# Matches the first non-comment, non-whitespace keyword of a statement -
# used by execute() below to reject anything that isn't a read. Strips SQL
# line comments ("--...") and block comments ("/*...*/") first so a
# statement like "-- get sales\nDELETE FROM orders" can't slip the read-only
# check by hiding its real first keyword behind a comment.
_LEADING_COMMENT_RE = re.compile(r'^\s*(--[^\n]*\n|/\*.*?\*/\s*)*', re.DOTALL)
_FIRST_KEYWORD_RE = re.compile(r'^\s*(\w+)', re.IGNORECASE)

# The only statement shapes MongoDB's SQL Interface actually supports (see
# this module's docstring) - "WITH" covers a leading common-table-expression
# ahead of a SELECT, same as every other ANSI-SQL dialect in this app.
_ALLOWED_LEADING_KEYWORDS = {"select", "with"}

# Must match the Dockerfile's odbcinst.ini registration ("[MongoDB Atlas SQL
# ODBC Driver]" section header) verbatim - this is a fixed, server-side
# detail of how the driver happens to be registered on this machine, not
# user data, so it's injected here rather than asked of a preset author or
# a custom-connection user (see this module's docstring).
_DRIVER_NAME = "MongoDB Atlas SQL ODBC Driver"
_DRIVER_CLAUSE = f"Driver={{{_DRIVER_NAME}}};"

# Strips a leading "Driver={...};" clause a caller's url might already
# carry (Atlas's own connect-modal snippet includes one, and so might an
# older saved preset/custom connection from before this was made
# automatic) so _build_connection_string can inject the canonical one
# without ever ending up with two.
_LEADING_DRIVER_CLAUSE_RE = re.compile(r'(?i)^\s*Driver\s*=\s*\{[^}]*\}\s*;?\s*')

# Recognizes a bare mongodb:// deployment URI at the very start of a
# (Driver=-stripped) url - a mongodb connection URI's own grammar never
# starts with "Uri=" itself, so this can only match the user-supplied
# "bare URI first" shape this module's docstring describes, never a false
# positive off Database=/User=/Password= (which always come later in the
# string, never at position 0).
_LEADING_MONGODB_URI_RE = re.compile(r'(?i)^\s*mongodb(\+srv)?://')


def _normalize_url(url):
    """Inserts the "Uri=" key name in front of `url` if it's missing - i.e.
    accepts both the "bare URI first" shape this app's UI now asks for
    (mongodb://...;Database=...;User=...;Password=...) and the fully
    explicit "Uri=mongodb://...;..." shape (Atlas's own connect-modal
    snippet, or an older saved preset/custom connection), normalizing both
    to the explicit-key form cache_key()'s and _build_connection_string()'s
    own regexes expect. A url that already starts with "Uri=" is returned
    unchanged - _LEADING_MONGODB_URI_RE only matches a bare URI at
    position 0, which "Uri=..." itself never is."""
    url = url or ''
    if _LEADING_MONGODB_URI_RE.match(url):
        return 'Uri=' + url
    return url


def _build_connection_string(url):
    """Returns the actual string handed to pyodbc.connect(): the canonical
    _DRIVER_CLAUSE prepended to `url` with any Driver=... clause `url`
    already contained stripped first (see this module's docstring for why
    Driver is never treated as user-supplied), and with _normalize_url()
    applied so a bare leading mongodb:// URI gets its "Uri=" key name back."""
    without_driver = _LEADING_DRIVER_CLAUSE_RE.sub('', url or '')
    return _DRIVER_CLAUSE + _normalize_url(without_driver)


def _packed_url(descriptor):
    """Reassembles a descriptor's separate url/database/user/password
    fields (see this module's docstring) back into the single semicolon-
    joined string _build_connection_string()/cache_key() already know how
    to finish (Driver= injection, Uri= key-name insertion) - connect() and
    cache_key() both call this first so neither has to know about two
    different descriptor shapes itself.

    Detection rule for which shape `descriptor` actually is: if any of
    "database"/"user"/"password" is present, this is the current
    four-field shape, and `url` (assumed bare - see this module's
    docstring) gets those fields appended. Otherwise `descriptor["url"]`
    is returned completely unchanged - covering an older saved custom
    connection or preset from before this dialect had separate fields,
    whose "url" alone still carries everything (Database=/User=/Password=,
    and possibly its own Driver=/Uri= prefixes too - see
    _build_connection_string()'s and _normalize_url()'s own backward-
    compatibility handling for those)."""
    descriptor = descriptor or {}
    url = descriptor.get("url") or ''
    database = descriptor.get("database")
    user = descriptor.get("user")
    password = descriptor.get("password")
    if not (database or user or password):
        return url
    packed = url
    if database:
        packed += f';Database={database}'
    if user:
        packed += f';User={user}'
    if password:
        packed += f';Password={password}'
    return packed


def _reject_if_not_read_only(stmt_clean):
    """Raises ValueError if `stmt_clean` isn't a SELECT (or a WITH ... SELECT
    CTE) - see this module's docstring for why MongoSQL has no write path at
    all. Checked here (rather than just letting the driver reject a write on
    its own) so a user gets yDyL's own clear message instead of whatever raw
    ODBC error text the driver happens to surface for an unsupported
    statement type."""
    stripped = _LEADING_COMMENT_RE.sub('', stmt_clean)
    match = _FIRST_KEYWORD_RE.match(stripped)
    keyword = (match.group(1) if match else "").lower()
    if keyword not in _ALLOWED_LEADING_KEYWORDS:
        raise ValueError(
            "MongoDB Atlas SQL is read-only - only SELECT (or WITH ... SELECT) "
            f"queries are supported, not '{keyword or stmt_clean[:20]}'."
        )


class MongoSqlBackend(Backend):
    dialect_name = "MongoDB Atlas SQL"

    def connect(self, descriptor):
        # _packed_url() reassembles the four descriptor fields (or passes
        # an older all-in-one "url" through unchanged - see its docstring)
        # into the one string _build_connection_string() finishes off with
        # the injected Driver=/Uri= pieces.
        full_conn_str = _build_connection_string(_packed_url(descriptor))
        # pyodbc's own connect-phase timeout kwarg - bounds dialing/
        # handshake only, same contract as every other backend's use of
        # DB_CONNECT_TIMEOUT_SECONDS (see backends/base.py's docstring).
        conn = pyodbc.connect(full_conn_str, timeout=DB_CONNECT_TIMEOUT_SECONDS, autocommit=True)
        return conn

    def close(self, connection):
        # hasattr-guarded like every other backend's close() - see
        # backends/postgres.py's close() docstring for why (tests patching
        # connect() with a lightweight stand-in, execute_routes.py's
        # unconditional finally-block call).
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    def cache_key(self, descriptor):
        """Uri=.../Database=... pulled out of the (reassembled - see
        _packed_url()) connection string - never the whole string, since
        that also carries User=/Password= (see every other backend's
        cache_key() docstring for why a credential can never end up here:
        this is logged, and used as a schema_cache.py key). Uri= here is
        the full mongodb:// deployment URI (there's no bare "Server=" host
        key in this driver's connection string - see this module's
        docstring) - it never carries the username/password itself in
        this driver's connection-string shape (those are always separate
        User=/Password= keys), so including it verbatim in the cache key
        is safe. Falls back to a fixed placeholder (still credential-free)
        if the string doesn't parse as expected, rather than raising - a
        malformed/unusual connection string should degrade to "no useful
        cache key", never to leaking part of it."""
        # _normalize_url() so this also finds the Uri= value when the
        # reassembled string is the bare-URI-first shape (mongodb://...;
        # Database=...;...) rather than the fully explicit
        # "Uri=mongodb://..." shape.
        url = _normalize_url(_packed_url(descriptor))
        uri_match = re.search(r'(?i)\bUri\s*=\s*([^;]+)', url)
        db_match = re.search(r'(?i)\bDatabase\s*=\s*([^;]+)', url)
        uri = uri_match.group(1).strip() if uri_match else "unknown"
        database = db_match.group(1).strip() if db_match else "unknown"
        return f"{uri}/{database}"

    def identity_label(self, connection):
        """(database, username) via SQLGetInfo - SQL_DATABASE_NAME (16) and
        SQL_USER_NAME (47) are standard ODBC info types every conformant
        driver (including MongoDB's) implements, not Mongo-specific
        constants - same idea as backends/postgres.py's
        "current_database(), CURRENT_USER", just fetched through the ODBC
        driver-manager layer instead of a SQL query, since MongoSQL has no
        guarantee those two particular function names exist."""
        try:
            db_name = connection.getinfo(pyodbc.SQL_DATABASE_NAME) or "Unknown"
            username = connection.getinfo(pyodbc.SQL_USER_NAME) or "Unknown"
            return db_name, username
        except Exception:
            return "Unknown", "Unknown"

    def get_schema(self, connection):
        """Collection/column introspection via the ODBC driver's own
        standard catalog functions (SQLTables/SQLColumns, exposed by pyodbc
        as cursor.tables()/cursor.columns()) rather than any Mongo-specific
        query - these are part of the ODBC spec itself, not something
        MongoDB's driver invented, so this works the same way it would for
        any other ODBC-only dialect this app might add later. MongoDB
        Atlas SQL exposes each collection as one "table"; a document's
        nested fields are flattened/typed by the driver's own schema
        inference (see MongoDB's SQL Interface docs), so a "column" here is
        really "a field path the driver has already inferred a type for" -
        this backend has no visibility into (and no need to reproduce)
        however that inference actually works."""
        cursor = connection.cursor()

        # Phase 1: cheap - just the collection ("table") names, bounded the
        # same way backends/postgres.py's phase 1 is (see
        # SCHEMA_MAX_TABLE_NAMES_SCANNED's docstring in backends/base.py).
        # tableType='TABLE' excludes views/system tables the driver might
        # otherwise also list.
        all_table_names = []
        for row in cursor.tables(tableType='TABLE'):
            all_table_names.append(row.table_name)
            if len(all_table_names) >= SCHEMA_MAX_TABLE_NAMES_SCANNED:
                break

        if not all_table_names:
            return None

        # No date-shard collapsing here (unlike postgres.py/bigquery.py) -
        # date-sharded collection families aren't a convention MongoDB users
        # actually follow the way BigQuery's own docs recommend it, so
        # there's nothing to collapse; just cap the plain count.
        kept_names = sorted(all_table_names)
        omitted_count = 0
        if len(kept_names) > SCHEMA_MAX_TABLES:
            omitted_count = len(kept_names) - SCHEMA_MAX_TABLES
            kept_names = kept_names[:SCHEMA_MAX_TABLES]

        schema_parts = []
        for table_name in kept_names:
            col_defs = []
            for col in cursor.columns(table=table_name):
                null_str = "NULL" if getattr(col, "is_nullable", "YES") != "NO" else "NOT NULL"
                col_defs.append(f"  {col.column_name} {col.type_name} {null_str}")
            if col_defs:
                schema_parts.append(f"Table: {table_name}\n" + "\n".join(col_defs))

        if omitted_count:
            schema_parts.append(
                f"[... {omitted_count} more table(s) not shown - this schema has "
                f"more than the {SCHEMA_MAX_TABLES}-table summary limit. Ask about "
                f"a narrower set of collections to see the rest.]"
            )

        cursor.close()

        if not schema_parts:
            return None
        return cap_schema_text("\n\n".join(schema_parts))

    def execute(self, connection, sql_text):
        statements = [s.strip() for s in sqlparse.split(sql_text) if s.strip()]
        results = []

        cursor = connection.cursor()
        try:
            for stmt in statements:
                stmt_clean = stmt.rstrip(';').strip()
                if not stmt_clean:
                    continue

                try:
                    _reject_if_not_read_only(stmt_clean)
                    cursor.execute(stmt_clean)

                    columns = None
                    rows = None
                    count = 0

                    if cursor.description:
                        columns = [desc[0] for desc in cursor.description]
                        rows = []
                        for r in cursor.fetchall():
                            row_dict = {}
                            for idx, col in enumerate(columns):
                                val = r[idx]
                                if hasattr(val, 'isoformat'):
                                    val = val.isoformat()
                                elif isinstance(val, bytes):
                                    val = val.decode('utf-8', errors='replace')
                                elif type(val).__name__ == 'Decimal':
                                    val = float(val)
                                row_dict[col] = val
                            rows.append(row_dict)
                        count = len(rows)

                    results.append({
                        'statement': stmt_clean,
                        'columns': columns,
                        'rows': rows,
                        'rowCount': count,
                    })
                except Exception as e:
                    # Same partial-results preservation as every other
                    # backend - see SqlExecutionError's docstring in
                    # backends/base.py. Covers both a real driver error and
                    # _reject_if_not_read_only's ValueError uniformly.
                    raise SqlExecutionError(str(e), results, stmt_clean, len(results), len(statements)) from e
        finally:
            cursor.close()

        return results
