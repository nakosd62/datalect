"""
backends/base.py

Abstract interface every supported database dialect implements. db.py and
execute_routes.py talk to backends only through this interface, so adding
a new dialect (BigQuery, Snowflake, Databricks, ...) means adding one new
file in this package and registering it in backends/__init__.py, not
touching the route/dispatch layer or duplicating connection-handling,
schema-introspection, or query-execution logic per route.

A "connection descriptor" is the small dict that routes/db.py pass around
to identify which database to talk to. Every descriptor has a "type" key
(e.g. "postgres") that backends/__init__.py uses to pick the right
Backend. The rest of the shape is backend-specific:
  - PostgresBackend expects {"type": "postgres", "url": "postgresql://..."}
  - A future BigQueryBackend is free to define its own fields (project_id,
    dataset, credentials_ref, ...) rather than being forced into a
    connection-string shape that doesn't really fit BigQuery's auth model.

Today every descriptor in the app is built by wrapping a plain Postgres
connection-string (see db.py's `_to_descriptor`) - state_store, the
frontend, and CONFIGURED_DBS all still deal purely in URL strings. That's
deliberate: there's no second backend yet to justify reshaping those
layers, so this refactor only introduces the dispatch seam. Propagating
richer descriptors (type selection, non-URL credentials) up through
state_store/the API/the UI is follow-up work for when a second backend
actually needs it.
"""

import math
import os
import re
import tempfile
from abc import ABC, abstractmethod

# --- Schema-introspection size limits ---------------------------------------
# A dataset/schema with an unbounded number of tables can make get_schema()
# (below, implemented per-backend) intractable: the introspection query
# itself can return an enormous row set, the resulting text can blow past
# what's sane to embed in every single /api/translate prompt (cost and
# latency scale with it directly), and it's also cached as-is in
# schema_cache.py. These are shared across every backend so the protection
# is consistent regardless of dialect, and env-configurable (like
# SCHEMA_SHARD_MIN_GROUP_SIZE below) so a deployment with unusually large
# schemas can tune them without a code change.

# Hard cap on how many distinct table "entries" (a plain table, or one
# collapsed date-shard family - see group_date_sharded_tables below) a
# schema description will ever describe. Whatever's kept is chosen
# deterministically (alphabetically) so repeated calls against an unchanged
# schema return the same subset rather than an arbitrary one.
SCHEMA_MAX_TABLES = int(os.environ.get("SCHEMA_MAX_TABLES", 200))

# Hard cap on the number of raw table names even scanned before grouping/
# capping happens - protects the cheap "just list the table names" query
# itself from returning an unbounded row set on a truly extreme schema
# (tens/hundreds of thousands of tables), independent of SCHEMA_MAX_TABLES.
SCHEMA_MAX_TABLE_NAMES_SCANNED = int(os.environ.get("SCHEMA_MAX_TABLE_NAMES_SCANNED", 20_000))

# Final backstop on the assembled schema text's total length, regardless of
# what caused it to grow (huge column counts on a handful of tables, huge
# view definitions, more table entries than fit even after the caps above).
SCHEMA_MAX_CHARS = int(os.environ.get("SCHEMA_MAX_SCHEMA_CHARS", 100_000))

# When true, translate_routes.py feeds the LLM the "tables_only" schema
# derivative (see derive_tables_only_schema_text below) instead of the full
# deep schema text for every /api/translate (and related) call - identical
# per-table detail (columns, types, everything else under a table's own
# heading), but every OTHER top-level schema-object section (Constraints,
# Indexes, Views, Grants, and so on) stripped out, purely to cut down input
# token cost. Off by default, so an unset/blank/anything-other-than-"true"
# value leaves every existing deployment's prompts byte-for-byte unchanged.
# Read once at import time like every other SCHEMA_* flag here, so flipping
# it requires a process restart, same as the numeric caps above.
SCHEMA_TABLES_ONLY = os.environ.get("SCHEMA_TABLES_ONLY", "").strip().lower() == "true"

# --- Frequent-value sampling threshold ---------------------------------------
# Every backend's own Phase 2 "Column value samples" section picks each
# categorical column's top FREQUENT_VALUES_LIMIT values by plain COUNT(*) -
# used to be redefined locally as a plain `FREQUENT_VALUES_LIMIT = 15` in
# each of the 8 backends that have this section (postgres/redshift/mysql/
# snowflake/databricks/bigquery/mssql/oracle - mongodb_sql.py and sheets.py
# have no equivalent sampling pass at all), identically everywhere, so it's
# centralized here instead - same reasoning as SCHEMA_MAX_CHARS above, and
# the same move FREQUENT_VALUE_MIN_FRACTION just below made for the
# per-value share-of-table floor: one shared, env-configurable knob rather
# than 8 copies of the same literal that could quietly drift out of sync.
FREQUENT_VALUES_LIMIT = int(os.environ.get("SCHEMA_FREQUENT_VALUES_LIMIT", 15))

# "sorts into the top N" and "is actually common" are different things: a
# column with thousands of near-evenly-distributed distinct values (e.g. a
# high-cardinality but not-quite-unique code) would still surface its top
# FREQUENT_VALUES_LIMIT values there, each barely a fraction of a percent of
# the table, presented to the model with no indication they're not
# representative at all. This is a second, independent gate applied on top
# of that existing top-N limit (a value must clear BOTH to be shown) - how
# big a share of the table's own live row count a value's own COUNT(*) has
# to reach before it even counts as "frequent" to begin with. Shared here
# (like SCHEMA_MAX_CHARS above) rather than redefined per backend, so every
# dialect applies the identical threshold and a deployment can tune it once,
# everywhere, via the env var.
FREQUENT_VALUE_MIN_FRACTION = float(os.environ.get("SCHEMA_FREQUENT_VALUE_MIN_FRACTION", 0.05))


def min_frequent_value_count(row_count):
    """The minimum COUNT(*) a value needs to clear FREQUENT_VALUE_MIN_FRACTION
    for a table with `row_count` live rows, as a plain int suitable for a
    HAVING COUNT(*) >= <this> clause - always at least 1 once `row_count` is
    usable, so a zero-count value is never reported as "frequent" no matter
    how tiny the table is.

    Returns None - meaning "no HAVING floor; keep the old top-N-only
    behavior for this table" - when `row_count` isn't a usable positive
    number. Every backend's own live COUNT(*) query (the one that already
    produces its "Live row counts" line) can fail or return nothing on a
    connection hiccup, exactly as tolerated there; falling back to "no
    minimum" here rather than e.g. treating a missing count as 0 (which
    would make EVERY value fail the >= check and silently drop the whole
    "Column value samples" section for that table) keeps this purely
    additive - a table this can't compute a percentage for still gets the
    exact same sampling it always did."""
    if not isinstance(row_count, (int, float)) or row_count <= 0:
        return None
    return max(1, math.ceil(row_count * FREQUENT_VALUE_MIN_FRACTION))


# --- Connect-time timeout ----------------------------------------------------
# Bounds how long connect() may block dialing/handshaking out to a real
# network host - Postgres, MySQL, Redshift, Oracle, and Snowflake all thread
# this through to their respective driver's own connect-phase timeout kwarg
# (see each backend's connect()). SQL Server (backends/mssql.py) also passes
# it through as pytds's own login_timeout, but - unlike those five drivers -
# pytds doesn't reliably honor it as a hard deadline (a real accounting bug
# in its own retry/backoff loop lets total wall-clock time run well past the
# configured budget - confirmed live against a since-unreachable Azure SQL
# preset), so that backend ALSO wraps the call in its own external
# ThreadPoolExecutor/future.result(timeout=...) enforcement (see
# backends/mssql.py's _connect_with_hard_timeout) - the same "the driver's
# own timeout can't be trusted" fix execute_routes.py's _execute_with_timeout
# already applies to query execution. Without it, a wrong/unreachable host in an
# admin-configured preset (bad hostname, closed security group, blackholed
# route - exactly the DNS-resolution/connection-timeout failures worked
# through live against a real Redshift Serverless workgroup) doesn't fail
# fast: it hangs for however long the OS's own TCP connect timeout happens
# to be, which is unbounded in practice (a silently-dropped SYN can hang for
# minutes). Every place that calls a backend's connect() - /api/config's
# identity probe, /api/ping's liveness check, /api/execute, /api/translate's
# schema fetch - already wraps it in try/except and degrades gracefully
# (see execute_routes.py's ping()/config_routes.py's handle_config()), so
# once connect() itself is bounded, one bad preset's connection attempts
# fail in DB_CONNECT_TIMEOUT_SECONDS instead of hanging indefinitely.
#
# That said, this alone only bounds *how long* one bad preset's connection
# attempt can block - it doesn't change *what else* is blocked meanwhile.
# Werkzeug's dev server (what server.py actually runs, in production too -
# see the Dockerfile's CMD) handles one request at a time unless told
# otherwise, so a single slow connect() - even a bounded one - still stalls
# every other user's unrelated request for that whole window. See
# server.py's threaded=True for the other half of this fix: bounding the
# *duration* of a bad connection here, and bounding its *blast radius* there.
#
# Deliberately one shared, env-configurable knob rather than a per-dialect
# env var - the failure mode is identical regardless of which dialect a
# preset happens to be. Scoped to connection establishment only: every kwarg
# it's passed as below (psycopg2's/pymysql's connect_timeout, oracledb's
# tcp_connect_timeout, Snowflake's login_timeout) is documented by its own
# driver as bounding only the initial connect/handshake phase, never query
# execution afterwards - a slow-but-legitimate query against an already-open
# connection is unaffected. Databricks is the one dialect this doesn't cover
# (see backends/databricks.py's connect() for why its driver has no
# equivalent connect-only knob to hook into) and BigQuery doesn't need it
# (bigquery.Client() construction doesn't dial out synchronously the way a
# real TCP connect() does).
DB_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", 10))


def resolve_timeout_seconds(descriptor, field_name, default_seconds):
    """Returns the effective timeout (seconds, float) for one connection
    descriptor: its own per-dataset override at `field_name` if present
    and a valid positive number, else `default_seconds` unchanged.

    This is what lets a single admin-configured preset or a single user's
    custom connection override either of this app's two global,
    env-configurable timeout knobs - DB_CONNECT_TIMEOUT_SECONDS above (via
    a descriptor's own "connect_timeout_seconds") or execute_routes.py's
    SQL_EXECUTE_TIMEOUT_SECONDS (via "execute_timeout_seconds") - for just
    that one dataset, e.g. a data warehouse that's reliably slower to
    dial or to run a query against than every other configured database,
    without having to raise either shared knob (and therefore every OTHER
    dataset's own budget) app-wide just to accommodate the one slow
    outlier. See app_config.py's DATABASE_PRESETS_FILE comment and
    config_routes.py's module docstring for how an admin/user actually
    sets either field - both ultimately land as plain keys directly on
    the descriptor dict this function reads, exactly like every other
    per-connection field (url, schema, ca_cert_pem, ...).

    Every caller passes its own already-resolved global default in
    (DB_CONNECT_TIMEOUT_SECONDS for a connect()-time call, execute_routes.py's
    module-level SQL_EXECUTE_TIMEOUT_SECONDS for an execute()-time call)
    rather than this function importing either itself, so this stays a
    single, dependency-free helper both backends/*.py (connect-time) and
    execute_routes.py (execute-time) can share with no import-direction
    concerns between the two.

    A blank/missing override (the overwhelming common case - nobody sets
    this) falls through to `default_seconds` unchanged, exactly as if
    this function didn't exist. An override that doesn't parse as a
    number at all (a stray non-numeric string from a hand-edited presets
    file - the frontend's own <input type="number"> already guards
    against this in practice for a custom connection) is treated the same
    as "not set" - silently falling back to the shared default - rather
    than raising and taking the whole connect()/execute() attempt down
    over a typo in an optional field. A parsed value that isn't positive
    (0 or negative) is ALSO treated as "not set", falling back to
    `default_seconds` - unlike SQL_EXECUTE_TIMEOUT_SECONDS's own env-var-
    level "0 disables the timeout entirely" convention, a per-dataset
    override exists to make one dataset's timeout DIFFERENT from the
    shared default, not to let one dataset opt out of having a timeout at
    all; a deployment that actually wants no execute timeout anywhere
    still does that via the shared SQL_EXECUTE_TIMEOUT_SECONDS env var,
    unaffected by anything a preset/custom connection sets here."""
    override = (descriptor or {}).get(field_name)
    if override is None or override == "":
        return default_seconds
    try:
        value = float(override)
    except (TypeError, ValueError):
        return default_seconds
    return value if value > 0 else default_seconds


# Hard cap on how many table/tab names extract_entry_names_from_schema_text
# (below) returns for one connection's schema - independent of
# SCHEMA_MAX_TABLES (which bounds the full, column-level schema text this
# is extracted FROM). This caps the router's own prompt size (see
# connection_router.py's Phase A - it only ever needs table/tab NAMES, not
# columns, to guess which connection(s) a question is about), which
# otherwise would scale with however many entries survived
# SCHEMA_MAX_TABLES on every in-scope connection at once, not just one.
ROUTER_MAX_TABLE_NAMES_PER_CONNECTION = int(
    os.environ.get("ROUTER_MAX_TABLE_NAMES_PER_CONNECTION", 200)
)

# Minimum number of same-prefix, date-suffixed tables before they're treated
# as a date-shard family and collapsed into one schema entry (see
# group_date_sharded_tables). A table that just happens to end in a
# date-shaped number shouldn't be swept into a "family" of one.
SCHEMA_SHARD_MIN_GROUP_SIZE = int(os.environ.get("SCHEMA_SHARD_MIN_GROUP_SIZE", 3))

# Matches <prefix>_<date> where date is YYYYMMDD, YYYYMM, a bare YYYY
# (year 1900-2099 only - see below), YYYY-MM-DD, YYYY_MM_DD, or
# p<YYYY>_<MM> - the common date-sharding conventions (BigQuery's own docs
# use YYYYMMDD; the others show up often enough in hand-rolled sharding to
# be worth covering; p<YYYY>_<MM>, e.g. "payment_p2023_10", is Postgres
# declarative-partitioning's own common naming convention for monthly
# range partitions - a literal "p" followed by a zero-padded year_month,
# as opposed to the other alternatives which are all-digit). Prefix is
# matched non-greedily so a prefix that itself contains underscores (e.g.
# "raw_events_20240101" -> prefix "raw_events") still resolves to the
# longest non-date-shaped prefix via backtracking, not just the text
# before the first underscore - this is also what correctly resolves
# "payment_p2023_10" to prefix "payment" rather than swallowing the
# "p2023_10" partition suffix into the prefix. The month is required to be
# exactly 2 digits (zero-padded) so that group_date_sharded_tables' plain
# alphabetical sort (used both to pick the "most recent" representative
# and to order shard_groups' member list) stays chronological - an
# unpadded "p2023_9" would otherwise sort after "p2023_10" as a string,
# even though September precedes October.
#
# The bare-YYYY alternative is deliberately constrained to (19|20)\d{2}
# (1900-2099), not an unconstrained \d{4}: a 4-digit suffix is far more
# likely than a 6- or 8-digit one to collide with an unrelated numeric ID
# a table family just happens to be suffixed with (e.g. "chunk_0001" ..
# "chunk_0099") - the extra two digits in YYYYMM/YYYYMMDD already make
# those far less ambiguous on their own. Yearly-partitioned tables (e.g. a
# BigQuery dataset's pageviews_2015 .. pageviews_2026) are exactly what
# this alternative exists to catch.
_DATE_SHARD_RE = re.compile(
    r'^(?P<prefix>.+?)_(?P<date>\d{8}|\d{6}|(?:19|20)\d{2}|\d{4}-\d{2}-\d{2}|\d{4}_\d{2}_\d{2}|p\d{4}_\d{2})$'
)


def group_date_sharded_tables(table_names, min_group_size=SCHEMA_SHARD_MIN_GROUP_SIZE):
    """Splits `table_names` into (kept_names, shard_groups):

    - shard_groups: {prefix: [sorted member table names]}, one entry per
      qualifying date-shard family (>= min_group_size same-prefix tables
      whose suffix looks like a date) - e.g. events_20240101 ..
      events_20241231 collapses to {"events": [...365 names...]}. The
      assumption (per the caller) is that every member of a family shares
      the same columns, so callers only need to introspect one
      representative member's schema, not all of them.
    - kept_names: every table that ISN'T part of a qualifying shard family,
      plus exactly one representative per family - the lexicographically
      last member, which for zero-padded date suffixes is also the most
      recent, on the theory that's the member most likely to reflect the
      family's current schema if it has drifted over time.

    This is a pure function over table names only (no I/O) - callers do
    their own (backend-specific) column introspection afterward, scoped to
    just `kept_names`, which is what actually bounds the expensive part of
    schema fetching on a dataset with a huge number of shards."""
    candidates = {}
    kept = []
    for name in table_names:
        m = _DATE_SHARD_RE.match(name)
        if m:
            candidates.setdefault(m.group("prefix"), []).append(name)
        else:
            kept.append(name)

    shard_groups = {}
    for prefix, members in candidates.items():
        if len(members) >= min_group_size:
            members.sort()
            shard_groups[prefix] = members
            kept.append(members[-1])
        else:
            kept.extend(members)

    return kept, shard_groups


def cap_kept_tables(kept_names, shard_groups, max_tables=SCHEMA_MAX_TABLES):
    """Applies the final SCHEMA_MAX_TABLES cap to a (kept_names,
    shard_groups) pair from group_date_sharded_tables, choosing
    deterministically (alphabetical) which entries survive, and drops any
    shard_groups entry whose representative didn't. Returns (kept_names,
    shard_groups, omitted_count) - omitted_count is how many entries (plain
    tables or whole shard families, each counted once) were cut, for the
    caller to mention in a truncation note rather than truncating silently."""
    kept_names = sorted(kept_names)
    omitted_count = 0
    if len(kept_names) > max_tables:
        omitted_count = len(kept_names) - max_tables
        kept_names = kept_names[:max_tables]
    kept_set = set(kept_names)
    shard_groups = {p: members for p, members in shard_groups.items() if members[-1] in kept_set}
    return kept_names, shard_groups, omitted_count


# Substring every backend's own "N more table(s)... not shown" note (built
# from cap_kept_tables' omitted_count above - see e.g. backends/postgres.py's
# own "if omitted_count:" block) is guaranteed to contain, regardless of the
# exact rest of that backend's wording. The SQL backends that go through
# cap_kept_tables all say "more table(s)/table-family(ies) not shown";
# mongodb_sql.py computes omitted_count itself (no date-shard families to
# speak of) and says "more table(s) not shown" - both contain this literal
# substring, so schema_text_has_omitted_tables below recognizes either
# wording without forcing every backend's note text to be reworded to
# match a single shared string. Deliberately not made into a single shared
# note-building helper the way cap_schema_text's own note is centralized -
# that would mean rewording every backend's already-tested note text for no
# behavioral gain, just to detect something a plain substring check already
# detects just as reliably.
_TABLE_TRUNCATION_MARKER = "more table(s)"


def schema_text_has_omitted_tables(text):
    """True if `text` is a get_schema() result that left out at least one
    table/table-family because SCHEMA_MAX_TABLES was exceeded (carries the
    "N more table(s)... not shown" note every backend appends via its own
    `if omitted_count:` block) - False for None/empty text or a schema
    that described every table it found. Mirrors schema_text_was_truncated
    just above: recognizes an existing note already embedded in the text
    rather than needing the original omitted_count value threaded all the
    way out to a caller that never otherwise sees it - db.py's
    _fetch_database_schema is the one place both this and
    schema_text_was_truncated get checked, right after backend.get_schema()
    returns, for the same reason: SCHEMA_MAX_TABLES truncation was
    previously visible only inside the prompt text itself, never in server
    logs."""
    return bool(text) and _TABLE_TRUNCATION_MARKER in text


# Leading text of the note cap_schema_text appends when it actually
# truncates - factored out as a single source of truth so
# schema_text_was_truncated() below recognizes exactly the note
# cap_schema_text produces, rather than a second, separately-maintained
# copy of the same literal string drifting out of sync with it.
_SCHEMA_TRUNCATION_MARKER = "[... schema truncated:"


def cap_schema_text(text, max_chars=SCHEMA_MAX_CHARS):
    """Hard backstop on the final assembled schema text's length, regardless
    of what caused it to grow. Truncates on a paragraph boundary where
    possible so the cut doesn't land mid-table-definition, and always
    appends a note - a silently truncated schema is far more dangerous than
    a visibly truncated one, since Gemini would otherwise generate SQL
    against tables/columns it can't actually see with no indication
    anything is missing."""
    if not text or len(text) <= max_chars:
        return text
    cut = text.rfind("\n\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    return (
        text[:cut]
        + f"\n\n{_SCHEMA_TRUNCATION_MARKER} exceeded "
        + f"{max_chars:,} characters. Ask about fewer tables at once, or "
        + "reduce SCHEMA_MAX_CHARS/SCHEMA_MAX_TABLES scope on this "
        + "connection's dataset, to see more of it.]"
    )


def schema_text_was_truncated(text):
    """True if `text` is a cap_schema_text() result that actually got cut
    (carries its truncation marker) - False for None/empty text or text
    that never hit the SCHEMA_MAX_CHARS ceiling. Recognizes cap_schema_text's
    own output rather than re-running it, so a caller already holding the
    final schema text (db.py's _fetch_database_schema, right after
    backend.get_schema() returns) can log/alert on a truncation event
    without needing the original uncapped text or a second pass over it.
    See _fetch_database_schema's own use of this: SCHEMA_MAX_CHARS was
    previously silent everywhere except the note embedded in the prompt
    text itself - visible to the model, but invisible to anyone checking
    server logs to see how often a given connection's schema actually hits
    the ceiling."""
    return bool(text) and _SCHEMA_TRUNCATION_MARKER in text


def format_bytes_human(num_bytes):
    """Formats a byte count as a short human string (e.g. "3.2 GB"), used
    only for display within a schema's own "Estimated dataset size" line
    (see format_dataset_size_line() below) - never for anything a query
    plan or byte-exact calculation depends on. Returns '' for None or a
    negative input rather than raising, since every caller treats this as
    a best-effort decoration on an already best-effort estimate."""
    if num_bytes is None or num_bytes < 0:
        return ""
    value = float(num_bytes)
    for unit in ("bytes", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024.0 or unit == "PB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024.0
    return ""  # pragma: no cover - unreachable, PB branch above always returns


def format_compact_count(n):
    """Shortens a large count to K/M/B with 3 significant digits (e.g.
    1234 -> "1.23K", 12345 -> "12.3K", 123456 -> "123K", 8500000 -> "8.50M") -
    used for the row count in format_dataset_size_line() below, which can
    otherwise run to 8+ digits for a genuinely large table and read as
    visual noise next to the rest of that line. Mirrors client.js's own
    formatCompactCount() (used there for the Schema Viewer's per-table row-
    count suffix) so a large row count reads the same way whether it's
    rendered server-side (this function, in the schema text handed to the
    LLM) or client-side - one shared convention, not two independently
    tuned ones. A plain, comma-grouped integer below 1000, where a unit
    suffix wouldn't save space and would just read oddly (e.g. "0.42K")."""
    try:
        num = float(n)
    except (TypeError, ValueError):
        return str(n)
    abs_num = abs(num)
    for unit_value, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs_num >= unit_value:
            scaled = num / unit_value
            scaled_abs = abs(scaled)
            decimals = 0 if scaled_abs >= 100 else (1 if scaled_abs >= 10 else 2)
            return f"{scaled:.{decimals}f}{suffix}"
    return f"{int(round(num)):,}"


def format_multiline_schema_entry_body(body):
    """Reindents a View/Routine definitions entry's body text - a view's
    SELECT text (e.g. Postgres's pg_get_viewdef()/information_schema.views.
    view_definition) or a routine's CREATE.../body text, straight from the
    database, exactly as that dialect's own formatter produced it - so
    every line after the first is guaranteed to start with at least four
    spaces of leading whitespace, regardless of whatever indentation (if
    any, and it varies a lot: some dialects pretty-print with consistent
    indentation, others hand back the original, arbitrarily-formatted
    CREATE statement verbatim) the database itself used.

    This matters for two independent reasons on the webClient Schema
    Viewer side (client.js): (1) parseSchemaViews()/parseSchemaRoutines()
    identify each entry by its own "  View <name>: "/"  <name>: " header
    line and treat every following line that does NOT look like a new
    entry's header as part of the CURRENT entry's body - a body line that
    itself happened to start at column zero would end that entry early and
    silently drop the rest of its own text; (2) isTopLevelSchemaSectionLine()
    (used to find where a top-level section like "View definitions:" itself
    ENDS) treats ANY column-zero line as the start of the NEXT section - a
    raw, unindented line buried inside one view's own multi-line SQL would
    truncate the whole section early, silently dropping every view/routine
    listed after it too, not just the one whose body caused it.

    Called on every backend's own view_lines/routine_body_lines
    construction (e.g. backends/postgres.py's "  View {name}: {...}" f-
    string) - the caller still owns the "  View {name}: "/"  {name}: "
    header prefix and the empty-body case (a NULL/empty definition, e.g. a
    role lacking the privilege to see it - see backends/postgres.py's own
    view_definition NULL comment); this only ever touches what comes after
    that prefix. Returns '' unchanged for an empty/None body - never turns
    "nothing to show" into a non-empty string."""
    body = (body or "").strip()
    if not body:
        return ""
    lines = body.split("\n")
    return "\n".join([lines[0]] + [f"    {line}" for line in lines[1:]])


def format_dataset_size_line(total_rows=None, total_bytes=None, note=None):
    """Builds the single "Estimated dataset size: ..." line each SQL
    backend's get_schema() (deep fetch) appends to its own schema_parts,
    from whatever subset of (schema-wide row count, schema-wide storage
    bytes) that backend's dialect can cheaply provide via catalog/metadata
    statistics - deliberately never a live per-table COUNT(*) scan across
    the whole schema (get_schema()'s existing per-table "live,
    authoritative" counts already do that, but only for the capped/kept
    subset of tables shown in the prompt text - a schema-wide total needs
    an aggregate that doesn't scan actual rows).

    Deliberately just the one headline figure, nothing more: a plain
    byte-size estimate (e.g. "~3.2 GB") when total_bytes is available -
    the row count a caller computed for its own gating logic (only calling
    this at all once it knows at least one table was found) is NOT also
    rendered here alongside a table count, since "~1.23M rows across 42
    tables (~3.2 GB estimated storage)" is more detail than this one-line
    summary needs. Falls back to a compact row count (e.g. "~1.23M rows")
    only when no byte figure exists at all for this call - see backends/
    databricks.py, which has no cheap byte source on any call, or a byte
    query that failed for a dialect that normally has one (e.g. backends/
    oracle.py's separate, independently-failable USER_SEGMENTS query).

    Returns '' (nothing to append) when there's truly nothing to report -
    total_rows and total_bytes both None - so every caller can
    unconditionally do `if line: schema_parts.append(line)` without its
    own "did we get anything" branch first. This is the right outcome for
    a dialect with no cheap schema-wide source at all (see backends/
    mongodb_sql.py, backends/sheets.py) - a missing line reads as "not
    available for this connection type", never a misleading zero.

    `note`, when given, is appended as a trailing parenthetical caveat
    specific to that dialect's own source for this number (e.g. "stats may
    be stale since last ANALYZE", or Databricks' "live count of shown
    tables only, not a schema-wide estimate" - see individual backends).
    """
    if total_bytes is not None:
        bytes_human = format_bytes_human(total_bytes)
        if bytes_human:
            line = f"Estimated dataset size: ~{bytes_human}"
            return f"{line} ({note})" if note else line
    if total_rows is not None:
        line = f"Estimated dataset size: ~{format_compact_count(total_rows)} rows"
        return f"{line} ({note})" if note else line
    return ""


# The reciprocal of format_dataset_size_line() above: pulls that same
# "Estimated dataset size: ..." line's own value back out of a full schema
# text, for a caller that wants just this one figure rather than the whole
# text (e.g. db.py's build_group_schema_summaries(), which reports each
# dataset-group member's own data size in the Schema Viewer's group table -
# see webClient/client.js's openGroupSchemaViewer()). Mirrors client.js's
# own parseSchemaDatasetSizeLine() exactly (same "^Estimated dataset size: "
# anchor, multiline mode, first match only) so the server and client always
# agree on what this line looks like - if one side's regex ever drifted from
# the other's, only one of them would still recognize a dialect's line.
# Returns None (not '') when the schema text has no such line at all (a
# dialect with no cheap schema-wide size source - see backends/sheets.py,
# backends/mongodb_sql.py - or a schema fetch that failed outright), so a
# caller can tell "no line to show" apart from "" being a real, empty figure.
_DATASET_SIZE_LINE_RE = re.compile(r"^Estimated dataset size: (.+)$", re.MULTILINE)


def parse_dataset_size_line(schema_text):
    m = _DATASET_SIZE_LINE_RE.search(schema_text or "")
    return m.group(1) if m else None


# Same flat, non-tokenizer approximation client.js's Schema Viewer uses for
# its "Schema Size: ... tokens" facts-line figure (SCHEMA_VIEWER_CHARS_PER_
# TOKEN there) - kept as one shared constant here so db.py's
# build_group_schema_summaries() (the dataset-group Schema Viewer's own
# per-dataset "Schema Size" column) reports the exact same number a user
# would see if they instead opened that one dataset on its own. Not a real
# tokenizer on either side - just len(schema_text) / 4.
SCHEMA_SIZE_CHARS_PER_TOKEN = 4

# Same quantization client.js's Schema Viewer applies to that same figure
# (SCHEMA_VIEWER_TOKEN_QUANTUM there) - the raw chars-per-4 estimate above
# is quantized UP to the nearest multiple of this, per an explicit request,
# since it's only ever a rough, order-of-magnitude cost estimate and a
# precise-looking exact figure would overstate how exact it actually is.
# Shared here for the same "the group table and the single-connection
# viewer must always show the identical number" reason as
# SCHEMA_SIZE_CHARS_PER_TOKEN itself - see quantize_schema_size_tokens()
# below, db.py's build_group_schema_summaries()'s only caller.
SCHEMA_SIZE_TOKEN_QUANTUM = 100


def quantize_schema_size_tokens(raw_token_count):
    """Rounds a raw chars-per-token schema-size estimate UP to the nearest
    SCHEMA_SIZE_TOKEN_QUANTUM (100 * CEIL(raw_token_count / 100)) - see
    that constant's own comment for why. `raw_token_count` is typically
    itself already a float (len(schema_text) / SCHEMA_SIZE_CHARS_PER_TOKEN)
    - this only ever returns a plain int, never negative (a 0-or-negative
    input, which shouldn't happen for any real schema_text but costs
    nothing to guard, returns 0 rather than a confusing negative multiple)."""
    if raw_token_count <= 0:
        return 0
    return SCHEMA_SIZE_TOKEN_QUANTUM * math.ceil(raw_token_count / SCHEMA_SIZE_TOKEN_QUANTUM)


# Column-name suffixes that conventionally mark a foreign-key-shaped column
# (customer_id, order_key, region_fk) - matched case-insensitively against
# the tail of a column name, longest suffix first so "_id" doesn't shadow a
# more specific match that happens to also end in "id" (there isn't one
# today, but ordering by length keeps this correct if a suffix is ever
# added). Not a personal name (e.g. a real "id" primary key column itself,
# with no suffix at all) - the exact-column-name path in
# find_naming_convention_relationships handles that case separately.
_FK_SUFFIXES = ("_id", "_key", "_fk")


def find_naming_convention_relationships(table_columns):
    """Phase 2, shared across every backend: a pure heuristic pass over
    already-fetched table/column names (no SQL - table_columns is
    {table_name: [column_name, ...]}, whatever Phase 1 already queried),
    returning "likely relationship, unconfirmed" strings for column-name
    patterns that look like an unenforced foreign key.

    Two independent heuristics, each conservative on purpose - false
    negatives (a real relationship this misses) are fine; false positives
    (telling the model two unrelated tables are linked) actively mislead
    query generation, so both require an exact name match against another
    table, not a fuzzy one:

    1. A column ending in _id/_key/_fk (see _FK_SUFFIXES) whose prefix
       exactly matches another table's name (singular or with a trailing
       "s" - "customer_id" -> "customers" or "customer") - e.g.
       orders.customer_id -> customers. This only checks that the target
       table exists by name (table_columns' minimal shape has no reliable
       cross-backend way to identify "the" PK column of an arbitrary
       table without a second, backend-specific query) - it does not
       verify the target table actually has a matching PK/id column, so
       it's a name-existence check, one notch looser than heuristic 2.
    2. A column with the exact same name in two different tables, where
       that name itself looks like a foreign-key-shaped identifier (ends
       in one of _FK_SUFFIXES, with a real prefix before the suffix) -
       e.g. two tables both having a "region_id" column, even if neither
       is named "regions". Deliberately excludes a *bare* suffix with no
       prefix - plain "id" (doesn't match any _FK_SUFFIXES entry at all)
       and MongoDB's own universal per-document "_id" field (matches
       "_id" but has an empty prefix) both fall under "virtually every
       table/collection conventionally has its own identifier column
       named exactly this," which is the norm, not a relationship signal
       - only heuristic 1 above (a name like "customer_id" pointing at a
       table actually named "customers") gets to use a bare identifier
       name as a match *target*.

    Every match is worded as "likely" and "unconfirmed" (see the returned
    string) precisely because this is a naming convention, not a real
    constraint lookup (see PK/FK/unique constraints, which come from an
    actual catalog query and need no such hedge) - callers must not
    present this with the same confidence as a real foreign key.

    Returns a list of human-readable strings, one per match, in a
    deterministic order (sorted by (referencing table, column) - table_columns'
    own dict/list ordering is preserved wherever possible, but the final
    sort makes output stable regardless of dict iteration order across
    Python versions/call sites). Pure function, no I/O, safe to call with
    any table_columns shape Phase 1 already assembled - callers pass
    whatever kept_names/columns data _build_shallow_schema_parts already
    produced, no new query."""
    if not table_columns:
        return []

    table_names = set(table_columns.keys())

    def _referenced_table(column_name):
        lower = column_name.lower()
        for suffix in _FK_SUFFIXES:
            if lower.endswith(suffix) and len(lower) > len(suffix):
                prefix = lower[: -len(suffix)]
                for candidate in table_names:
                    candidate_lower = candidate.lower()
                    if candidate_lower in (prefix, prefix + "s", prefix.rstrip("s")):
                        return candidate
        return None

    # name -> set of tables that have a column with exactly this name,
    # for heuristic 2 (same identifier-shaped column name in 2+ tables).
    columns_by_name = {}
    for table, columns in table_columns.items():
        for column in columns:
            columns_by_name.setdefault(column.lower(), set()).add(table)

    matches = []
    seen = set()

    for table, columns in table_columns.items():
        for column in columns:
            referenced = _referenced_table(column)
            if referenced and referenced != table:
                key = (table, column, referenced, "prefix")
                if key not in seen:
                    seen.add(key)
                    matches.append((
                        table, column,
                        f"{table}.{column} -> likely relationship (unconfirmed): "
                        f"references {referenced}, based on column naming "
                        f"convention only - no enforced foreign key found."
                    ))

    for name, tables in columns_by_name.items():
        # len(name) > len(suffix) excludes a *bare* suffix with no real
        # prefix before it - e.g. MongoDB's own universal per-document
        # "_id" field name is, by that dialect's convention, every single
        # collection's own identifier (the same role plain "id" plays in
        # an RDBMS, just spelled with the underscore folded into the
        # suffix instead of separated from it) - two collections both
        # having a bare "_id" column is the norm, not a relationship
        # signal, same reasoning as excluding plain "id" above. A real
        # prefix (e.g. "region_id") still qualifies.
        is_fk_shaped = any(name.endswith(s) and len(name) > len(s) for s in _FK_SUFFIXES)
        if is_fk_shaped and len(tables) > 1:
            for table in tables:
                key = (table, name, None, "shared-name")
                if key not in seen:
                    seen.add(key)
                    others = sorted(t for t in tables if t != table)
                    matches.append((
                        table, name,
                        f"{table}.{name} -> likely relationship (unconfirmed): "
                        f"same column name also appears in {', '.join(others)}, "
                        f"based on column naming convention only - no enforced "
                        f"foreign key found."
                    ))

    matches.sort(key=lambda m: (m[0], m[1]))
    return [text for (_table, _column, text) in matches]


# --- Query-execution row cap -------------------------------------------------
# Bounds how many rows a single statement's real result set can put into
# memory and into the HTTP response at all - a completely different thing
# from SUMMARY_RESULTS_MAX_ROWS (translate_routes.py), which only bounds
# what the post-execution summarization LLM call sees, never what actually
# gets returned to and rendered by the browser. Without a cap here, a query
# that genuinely matches millions of rows gets pulled entirely into a Python
# list via a bare cursor.fetchall() and then JSON-serialized into the HTTP
# response in one unbounded shot - an out-of-memory crash (or a response so
# large the browser/proxy chokes on it) waiting to happen the moment a user
# asks a broad enough question. Every backend's execute() must cap what it
# actually fetches, not just truncate an already-fully-materialized list
# afterward - that would still take the OOM hit before the truncation ever
# gets a chance to run. Standard DB-API-cursor backends (postgres/mysql/
# mssql/oracle/redshift/snowflake/databricks/mongodb_sql) get this via
# fetch_capped_rows() below; bigquery.py (a RowIterator, not a cursor) and
# sheets.py (a plain fetched table, no query-time LIMIT concept) apply the
# same cap their own way - see each one's own comment. One shared,
# env-configurable knob rather than a per-dialect constant, same posture as
# DB_CONNECT_TIMEOUT_SECONDS above - the failure mode is identical
# regardless of which dialect a preset happens to be.
EXECUTE_RESULTS_MAX_ROWS = int(os.environ.get("EXECUTE_RESULTS_MAX_ROWS", 500))


def normalize_cell_value(val):
    """Coerces one raw driver value into a JSON-safe Python value. Shared by
    every DB-API-style backend's row-building loop - previously duplicated
    nearly verbatim in each of postgres.py/mysql.py/mssql.py/oracle.py/
    redshift.py/snowflake.py/databricks.py (and in mongodb_sql.py, minus
    the to_eng_string branch below - an existing inconsistency this
    quietly fixes rather than preserves, since there's no reason a Mongo-
    via-ODBC decimal-like value should be normalized differently from every
    other dialect's). bigquery.py uses this too even though it never shared
    the fetch loop these came from (a RowIterator, not a cursor).

    Checked in order: anything with an isoformat() method (date/datetime/
    time objects, from every driver); anything with a to_eng_string()
    method (Python's own decimal.Decimal, and driver-specific numeric types
    that mimic it); raw bytes (decoded as UTF-8, replacing anything that
    isn't); and, as a last defensive catch-all, any other value whose type
    is literally named "Decimal" (a driver-specific decimal type that
    doesn't happen to implement to_eng_string). Everything else passes
    through unchanged."""
    if hasattr(val, 'isoformat'):
        return val.isoformat()
    if hasattr(val, 'to_eng_string'):
        return float(val)
    if isinstance(val, bytes):
        return val.decode('utf-8', errors='replace')
    if type(val).__name__ == 'Decimal':
        return float(val)
    return val


def fetch_capped_rows(cursor, max_rows=EXECUTE_RESULTS_MAX_ROWS):
    """For `cursor`, immediately after it ran a statement with a real
    result set (cursor.description is truthy), returns
    (columns, rows, truncated): `columns` from cursor.description, up to
    `max_rows` rows as {column: value} dicts with every value passed
    through normalize_cell_value, and `truncated` = True iff the cursor
    actually had at least one more row beyond `max_rows` still unread.

    Uses cursor.fetchmany(max_rows) rather than fetchall() - the entire
    point of EXECUTE_RESULTS_MAX_ROWS above is to never pull an unbounded
    result set into memory in the first place, so truncating an
    already-fully-fetched list afterward would defeat it. `truncated` is
    determined with exactly one extra cursor.fetchone() past the cap
    (discarded either way - never counted in `rows` or fetched again) -
    not by trusting cursor.rowcount, which several of these drivers don't
    reliably populate for a SELECT ahead of consuming the result set.

    Shared by every backend built on a standard DB-API-style cursor
    (postgres/mysql/mssql/oracle/redshift/snowflake/databricks/
    mongodb_sql) instead of each duplicating this fetch-and-normalize loop.
    bigquery.py and sheets.py have no such cursor (a RowIterator and a
    plain already-fetched table, respectively) and apply the same cap
    their own way instead - see each one's own comment."""
    columns = [desc[0] for desc in cursor.description]
    rows = []
    for r in cursor.fetchmany(max_rows):
        rows.append({col: normalize_cell_value(r[idx]) for idx, col in enumerate(columns)})
    truncated = len(rows) == max_rows and cursor.fetchone() is not None
    return columns, rows, truncated


# Matches the entry-heading convention every backend's get_schema() already
# emits, one per table/table-family/tab entry, always as the first line of
# that entry's block: "Table: <name>", "Table family: <name-pattern> (...)",
# or Sheets' "Tab: <name> (...)" (see e.g. backends/postgres.py, .../
# bigquery.py, .../sheets.py). This is already a load-bearing convention -
# the BigQuery dialect prompt in translate_routes.py references "Table
# family:" text directly - so parsing it here is leaning on an existing
# contract, not inventing a new coupling.
_ENTRY_HEADING_RE = re.compile(r'^(?:Table family|Table|Tab):\s*(.+)$', re.MULTILINE)


_ASIDE_BRACKETS = {")": "(", "]": "["}


def _strip_trailing_asides(text):
    """Strips every trailing "aside" chained onto the end of a heading,
    repeating until none are left - a descriptive parenthetical (every
    "Table family"/"Tab" heading has one, e.g. " (12 date-sharded tables,
    e.g. ...)" or " (query this as the implicit data source ...)"), a
    bracketed annotation (BigQuery's own get_schema() appends
    " [external table]" and/or " [REQUIRES PARTITION FILTER on <col>]"
    onto a heading that already ends in its own descriptive parenthetical -
    see that module's own heading_annotations handling), or both chained
    one after the other, in either order. A plain "Table: <name>" heading
    has neither and is returned completely unchanged. This is what lets a
    UI list row, the ER diagram's node label, and connection_router.py's
    Phase A candidate summaries all show just the bare name/pattern, never
    the full explanatory text (and now never a "[...]" annotation either)
    that's meant for the SQL-generation prompt - still available verbatim
    via split_schema_text_into_entries()'s own "heading"/"text" fields,
    untouched by this.

    Walks the string from the end counting bracket depth for whichever of
    ")"/"]" it currently ends with, rather than a single non-nesting regex
    (the earlier `\\([^)]*\\)$` this replaced) - a descriptive parenthetical
    can itself contain a nested, balanced parenthetical (BigQuery's own
    "Table family" heading does: "... filtering/identifying the shard via
    the _TABLE_SUFFIX pseudo-column (e.g. WHERE _TABLE_SUFFIX BETWEEN
    '...' AND '...'); never query a single literal date-suffixed table
    name from this family)" has a "(e.g. ...)" aside inside the outer
    parenthetical), which a non-nesting regex can never match at all -
    `[^)]*` can't cross the inner ")" to reach the real outer one, so the
    whole match fails and NOTHING gets stripped, leaving the full sentence
    as the "name". This walks the closing bracket back to its own matching
    opener by depth, however much nesting of THAT SAME bracket type is in
    between, and strips from there - then repeats, since a heading can end
    in a "(...)" immediately followed by a "[...]" (or, in principle, the
    other order), each its own separate, self-contained aside (BigQuery's
    heading_annotations bracket never contains a literal "(" or ")", and
    the descriptive parenthetical never contains a literal "[" or "]", so
    each pass only ever tracks the one bracket type it started with).

    Returns whatever's left unchanged (rather than guessing) as soon as it
    no longer ends in ")" or "]" at all, or the first time the bracket it
    does end with turns out unbalanced (more closes than opens) - the
    latter should never happen for a heading text any backend here
    actually emits, but this degrades to a no-op on the remaining text
    instead of stripping the wrong span if one someday does."""
    result = text
    while True:
        stripped = result.rstrip()
        if not stripped or stripped[-1] not in _ASIDE_BRACKETS:
            return result
        close_ch = stripped[-1]
        open_ch = _ASIDE_BRACKETS[close_ch]
        depth = 0
        for i in range(len(stripped) - 1, -1, -1):
            ch = stripped[i]
            if ch == close_ch:
                depth += 1
            elif ch == open_ch:
                depth -= 1
                if depth == 0:
                    result = stripped[:i].rstrip()
                    break
        else:
            return result


def extract_entry_names_from_schema_text(schema_text, max_names=ROUTER_MAX_TABLE_NAMES_PER_CONNECTION):
    """Cheap, loose extraction of every table/table-family/tab NAME (no
    columns) from a full schema_text already produced by some backend's
    get_schema() - used to build compact per-connection summaries for
    connection_router.py's Phase A (see that module), so the router prompt
    can stay small regardless of how wide any one connection's full schema
    is.

    Loose and line-oriented by design, not a real parser of get_schema()'s
    output: matches on the heading convention every backend already commits
    to (see _ENTRY_HEADING_RE above), which is simpler and less brittle
    than trying to reconstruct backend-specific structure here. Never
    raises - a schema_text that doesn't match this convention at all (a
    future backend that changes it, or the "No schema description
    available." failure placeholder from db.py) just yields an empty list,
    degrading connection_router.py's candidate summary to "name + dialect,
    no table names" rather than failing the whole request.

    Deterministic and capped: returns at most `max_names` entries, in the
    order they appear in schema_text (get_schema() already emits them
    alphabetically - see cap_kept_tables), so repeated calls against an
    unchanged schema return the same subset. A "Table family: <prefix>_
    <date> (...)" heading's captured name already IS the literal pattern
    text (e.g. "events_<date>"), not a real table name - intentional, since
    that's exactly what a human/model reading the summary should see: one
    entry standing in for the whole family, same as the full schema text
    itself does."""
    if not schema_text:
        return []
    names = []
    for match in _ENTRY_HEADING_RE.finditer(schema_text):
        name = _strip_trailing_asides(match.group(1)).strip()
        if name:
            names.append(name)
        if len(names) >= max_names:
            break
    return names


def split_schema_text_into_entries(schema_text):
    """Splits a full get_schema()/get_schema_shallow() text into one entry
    per table/table-family/tab, using the exact same heading convention
    extract_entry_names_from_schema_text (above) already recognizes -
    "Table: <name>", "Table family: <pattern> (...)", "Tab: <name> (...)"
    as the first line of each entry's block (see _ENTRY_HEADING_RE's own
    comment for the underlying contract every backend commits to). Written
    for the schema-viewer feature (config_routes.py's GET /api/schema),
    which needs each entry's own full text block (not just its bare name,
    which is all extract_entry_names_from_schema_text ever returns), but
    reuses that same regex rather than inventing a second, subtly
    different parsing pass over the same convention.

    Returns a list of {"name", "heading", "text"} dicts, in the order
    entries appear in schema_text (already alphabetical - see
    cap_kept_tables): "name" is the bare entry name/pattern with its
    descriptive parenthetical stripped (identical to what
    extract_entry_names_from_schema_text returns for the same heading),
    "heading" is the raw heading line as-is (parenthetical included, e.g.
    "Table family: events_<date> (12 date-sharded tables, e.g. ...)"), and
    "text" is this entry's complete block - its heading line through (but
    not including) the next entry's heading, trailing blank lines
    stripped - i.e. exactly the slice of schema_text a reader would need
    to see everything this one backend said about this one table (columns,
    types, constraints, indexes, sample values, live row counts,
    naming-convention relationships, or whichever subset that backend's
    get_schema() includes).

    Any text before the first heading is returned as one leading entry
    with name=None, heading=None - no backend emits a schema-level
    preamble before its first table today, so this is purely defensive;
    a caller building a browsable table list should skip entries with
    name=None, while a caller wanting the complete original text back
    unchanged can still reconstruct it by joining every entry's "text" in
    original order (interposing the blank lines this function strips is
    the one thing that join wouldn't exactly reproduce - immaterial for
    display, since a schema viewer renders one entry at a time).

    Never raises - a schema_text with no recognizable heading at all (the
    "No schema description available." failure placeholder from db.py, or
    a future backend that changes this convention) yields a single entry
    with name=None holding the complete text, same as the "preamble" case
    above - callers should treat name=None as "nothing structured to
    show", not as an error, and fall back to displaying "text" as-is."""
    if not schema_text:
        return []
    matches = list(_ENTRY_HEADING_RE.finditer(schema_text))
    if not matches:
        return [{"name": None, "heading": None, "text": schema_text}]
    entries = []
    if matches[0].start() > 0:
        preamble = schema_text[:matches[0].start()].rstrip("\n")
        if preamble:
            entries.append({"name": None, "heading": None, "text": preamble})
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(schema_text)
        block = schema_text[start:end].rstrip("\n")
        name = _strip_trailing_asides(match.group(1)).strip()
        entries.append({"name": name or None, "heading": match.group(0), "text": block})
    return entries


# Recognizes the start of the first NON-table top-level section in a full
# schema_text - "top-level" meaning flush against the left margin, as
# opposed to a continuation line belonging to the entry above it. Every
# backend indents every continuation line it ever emits - a column
# definition, a constraint/index/view/grant/trigger/comment/routine
# listing, a sampled value, a naming-convention relationship (see e.g.
# postgres.py's `f"  {col_name} {data_type} ..."`, mongodb_sql.py's
# `f"  {col.column_name} {col.type_name} ..."`, bigquery.py's nested
# column/struct lines) - by at least one leading space. The only other
# kind of unindented line get_schema()/get_schema_shallow() ever emits is
# a table/table-family/tab heading (_ENTRY_HEADING_RE above) or the
# "[... N more table(s)/table-family(ies) not shown ...]" overflow notice
# that can immediately follow the table loop - both excluded here via
# negative lookahead, since derive_tables_only_schema_text (below) wants
# to know where the first section that ISN'T one of those two things
# begins, without hardcoding the long, backend-specific list of actual
# non-table section names (Constraints/Indexes/Views/Grants/Triggers/
# Comments/Row count estimates/Routines/Session/View definitions/Routine
# definitions/Live row counts/Column value samples/Estimated dataset
# size/Likely relationships/... - mongodb_sql.py and sheets.py don't even
# have most of these, and a future backend could add new ones).
_NON_TABLE_TOP_LEVEL_LINE_RE = re.compile(
    r'^(?!\s)(?!\[\.\.\.)(?!(?:Table family|Table|Tab):).+$',
    re.MULTILINE,
)


def derive_tables_only_schema_text(schema_text):
    """Reduces a full deep schema_text down to just its leading run of
    table/table-family/tab entries - full per-table detail (columns,
    types, everything else indented under that table's own heading) kept
    exactly as-is, but every OTHER top-level schema-object section that
    follows (Constraints, Indexes, Views, Grants, Triggers, Comments, Row
    count estimates, Routines, Session, View definitions, Routine
    definitions, Live row counts, Column value samples, Likely
    relationships, and any backend-specific ones like Clustering keys or
    Partition columns) dropped entirely. This is the "tables_only" schema
    kind: a cheaper-to-send-to-an-LLM derivative of the already-cached
    deep schema, computed purely in memory - no new database round trip,
    no new schema_cache entry (see translate_routes.py's
    get_llm_schema_text, the only caller, gated by SCHEMA_TABLES_ONLY
    above).

    Relies on a structural fact confirmed across every backend's
    get_schema()/get_schema_shallow(): the Tables-and-Columns loop always
    runs FIRST and appends every table/table-family entry as one
    contiguous block, before any other section is even queried, let
    alone appended (mssql.py even reorders explicitly - `schema_parts =
    table_section_parts + schema_parts` - to guarantee this same
    invariant holds there too) - so the boundary between "all the
    tables" and "everything else" is always a single point in
    schema_text, never interleaved. That's what lets this just find the
    first top-level line that ISN'T a table heading and ISN'T the
    "[... N more ... not shown ...]" overflow notice (itself about which
    tables were omitted, so kept), and truncate there, rather than
    needing to recognize every individual non-table section by name.

    Never raises. Returns schema_text (rstripped) unchanged if it's
    empty/falsy, or if no such non-table line is found at all - which
    happens for backends/schemas with no non-table sections to begin
    with (mongodb_sql.py, sheets.py), correctly a no-op rather than an
    error."""
    if not schema_text:
        return schema_text
    match = _NON_TABLE_TOP_LEVEL_LINE_RE.search(schema_text)
    if not match:
        return schema_text.rstrip()
    return schema_text[:match.start()].rstrip()


def materialize_ca_cert_tempfile(ca_cert_pem):
    """Writes `ca_cert_pem` (PEM text, pasted by a user through the config
    modal and stored verbatim in database_config - see config_routes.py's
    module docstring and state_store.py's _CREDENTIAL_CONFIG_FIELDS, which
    deliberately does NOT include this field since a CA certificate is
    public information, not a secret) to a fresh, uniquely-named temp file
    and returns its path. Shared by backends/postgres.py (libpq's
    "sslrootcert") and backends/mysql.py (an ssl.SSLContext's cafile) -
    both dialects' underlying driver only accepts a CA cert as a
    filesystem path, never inline PEM content, so this is the one place
    that gap gets bridged for either of them.

    A fresh tempfile.mkstemp() call per connect() (not a shared/cached
    path) matters: connect() can run concurrently for different users or
    different connections (schema-cache refreshes, concurrent /api/execute
    calls, ...), and a shared path would let one connection's CA cert
    clobber another's mid-handshake. The caller is responsible for
    deleting the path this returns once the underlying driver's connect
    call has returned (success or failure) - see backends/postgres.py's
    and backends/mysql.py's connect() for the delete-in-a-finally
    pattern."""
    fd, path = tempfile.mkstemp(suffix=".pem", prefix="ydyl_ca_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(ca_cert_pem)
    except Exception:
        os.remove(path)
        raise
    return path


class SqlExecutionError(Exception):
    """Raised by Backend.execute() when one statement in a multi-statement
    script fails partway through - e.g. statement 2 of 4 has a syntax
    error. Every SQL-capable backend's execute() runs statements in a
    single ordered loop (see e.g. backends/postgres.py) and used to just
    let the driver's raw exception propagate straight out of that loop,
    silently discarding every result dict already collected for the
    statements that succeeded before the failure - execute_routes.py had
    no way to report them, and the UI had no way to show them.

    Wrapping the failure in this instead preserves that partial state so
    execute_routes.py can build a response with one entry per ATTEMPTED
    statement (successes + the failure), letting the client render one
    results tab per statement - the same tabbed UI as the all-succeeded
    case - with the failed statement's tab flagged, instead of a single
    opaque "Execution Error" that loses track of what did or didn't run.
    Statements after the failure are never attempted at all (correct
    behavior - a script shouldn't keep running after an error), so there's
    nothing to report for those; only `results` (before) + the failed
    statement itself are ever available here.
    """
    def __init__(self, message, results, failed_statement, statement_index, total_statements):
        super().__init__(message)
        #: list of {"statement", "columns", "rows", "rowCount"} dicts for
        #: every statement that completed successfully BEFORE the failure,
        #: in order - the same shape execute() always returns on success.
        self.results = results
        #: raw (semicolon-stripped) SQL text of the statement that failed.
        self.failed_statement = failed_statement
        #: 0-based position of the failed statement among every statement
        #: in the script - always equal to len(results), since backends
        #: stop at the first failure, but kept explicit for clarity at the
        #: call site rather than making execute_routes.py re-derive it.
        self.statement_index = statement_index
        #: how many statements sqlparse split the script into in total -
        #: lets the client/caller say "statement 2 of 4 failed" even
        #: though statements 3-4 were never attempted.
        self.total_statements = total_statements


class Backend(ABC):
    """One implementation per supported SQL dialect/database product."""

    #: Human-readable dialect name meant to be interpolated into the
    #: Gemini system prompt (e.g. "PostgreSQL", "BigQuery Standard SQL")
    #: so the model knows which SQL flavor to generate. Not wired into
    #: translate_routes.py yet - that prompt is still hardcoded to
    #: Postgres; parameterizing it by dialect_name is the next step after
    #: this refactor, once there's a second dialect to actually test it
    #: against.
    dialect_name = "SQL"

    #: Trivial, always-runnable statement used purely to check "is this
    #: connection alive" (see execute_routes.py's /api/ping) - needs no
    #: existing table/dataset and no special permissions, just a live
    #: connection. "SELECT 1" is valid ANSI SQL and works as-is for every
    #: backend here except Oracle, which has no SELECT-without-FROM form
    #: (a bare "SELECT 1" raises ORA-00923: FROM keyword not found where
    #: expected) - see backends/oracle.py's override. Was previously
    #: hardcoded client-side in client.js's checkDbStatus(), which is what
    #: let this Oracle gap slip through undetected: the client can't know
    #: a dialect's SQL quirks, only the backend that already encodes them
    #: everywhere else in this file can.
    liveness_sql = "SELECT 1"

    @abstractmethod
    def connect(self, descriptor):
        """Open and return a live connection/client for `descriptor`."""

    @abstractmethod
    def close(self, connection):
        """Release a connection returned by connect(). Must tolerate
        connection=None (no-op) so callers can always call it in a
        finally block without an extra guard."""

    @abstractmethod
    def get_schema(self, connection):
        """Return a text description of the schema (tables, constraints,
        indexes, views, grants, triggers, or the closest per-backend
        equivalents) suitable for inclusion in the Gemini prompt. Return
        None/empty if nothing could be introspected - the caller (db.py)
        owns deciding what fallback text to show and whether to cache it.

        This is the "deep" fetch (Phase 1 catalog-only content plus Phase 2
        live-query enrichment - sampling, min/max, cardinality, live row
        counts, full view/routine bodies, naming-convention relationship
        pass) - see get_schema_shallow() below for the catalog-only subset.
        Every existing caller of get_schema() (single-connection mode, and
        all-dbs Phase B generation for a connection the router actually
        selected) wants this one, unchanged from before this split
        existed."""

    @abstractmethod
    def get_schema_shallow(self, connection):
        """Return the Phase 1 (catalog-only, no live queries) subset of
        get_schema()'s text - same tables/columns/constraints/indexes plus
        the new catalog-only attributes (identity markers, comments, row-
        count estimates, routine existence+signature without bodies,
        distribution/partition/clustering keys, session facts, widened
        grants, RLS/external flags), but view/routine bodies are named/
        signatured only, never rendered in full here.

        Used for connections that may not even be selected for SQL
        generation - db.py's build_router_candidate_summaries() (all-dbs
        Phase A triage) calls this instead of get_schema() specifically so
        an all-dbs question against N connections doesn't pay Phase 2's
        live-query cost for the N-1 connections the router never routes
        to. get_schema() (deep) builds on top of the same catalog data
        this returns rather than re-querying it - see each backend's
        _build_shallow_schema_parts() private helper, which both this
        method and get_schema() call."""

    @abstractmethod
    def execute(self, connection, sql_text):
        """Run one or more statements in `sql_text` and return a list of
        {"statement", "columns", "rows", "rowCount"} dicts, one per
        statement - the same shape execute_routes.py has always returned
        to the frontend. A dict MAY also carry an optional "notices": list
        of str key - server-side text a statement itself produced outside
        its own result set (e.g. Oracle's DBMS_OUTPUT.PUT_LINE, captured by
        backends/oracle.py's execute() - see its _drain_dbms_output()) -
        entirely absent for a backend/statement with nothing to report,
        never an empty list. A dict MAY also carry an optional
        "truncated": True key - this statement's real result set had more
        rows than EXECUTE_RESULTS_MAX_ROWS (below), so `rows`/`rowCount`
        reflect only the first EXECUTE_RESULTS_MAX_ROWS of it, never the
        full set - entirely absent (never explicitly False) when nothing
        was cut off. Every backend built on a standard DB-API cursor gets
        this via fetch_capped_rows() (below); bigquery.py/sheets.py apply
        the same cap their own way (see each one's own comment) - all
        MUST cap what they actually fetch/build, not just what they
        report, since fetching an unbounded result set into memory (and
        then JSON-serializing all of it into the HTTP response) is itself
        the failure mode this exists to prevent, not just an oversized
        `rowCount`. execute_routes.py passes results through untouched, so
        both keys reach the client as-is; see webClient/client.js's
        renderTableResult() for how "truncated" is surfaced to the user
        (never silently - see that function's own comment on why). If a
        statement partway through fails, raise
        SqlExecutionError (see its docstring above) instead of letting the
        raw driver exception propagate directly, so the statements that
        succeeded before it aren't silently lost. A failure on the very
        first statement, or a backend with no multi-statement concept
        (e.g. sheets.py), MAY still just let the original exception
        propagate - execute_routes.py handles both: SqlExecutionError gets
        the richer partial-results response, anything else falls back to
        the plain {"success": false, "error": ...} shape it's always had."""

    @abstractmethod
    def identity_label(self, connection):
        """Return (db_name, username) - or the closest per-backend
        equivalents (e.g. BigQuery: project/dataset) - for display in
        /api/config as a "which DB am I talking to" sanity check."""

    @abstractmethod
    def cache_key(self, descriptor):
        """Return a short, non-sensitive string that uniquely identifies
        `descriptor` for schema_cache.py. Must never be, or leak, a
        credential (password, API key, service-account key, etc.) since
        this may end up in logs."""