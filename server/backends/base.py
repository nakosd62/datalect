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

import os
import re
from abc import ABC, abstractmethod

# --- Schema-introspection size limits ---------------------------------------
# A dataset/schema with an unbounded number of tables can make get_schema()
# (below, implemented per-backend) intractable: the introspection query
# itself can return an enormous row set, the resulting text can blow past
# what's sane to embed in every single /api/translate prompt (cost and
# latency scale with it directly), and it's also cached as-is in
# schema_cache.py. These are shared across every backend so the protection
# is consistent regardless of dialect, and env-configurable (like
# schema_cache.py's own SCHEMA_CACHE_TTL_SECONDS) so a deployment with
# unusually large schemas can tune them without a code change.

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

# Minimum number of same-prefix, date-suffixed tables before they're treated
# as a date-shard family and collapsed into one schema entry (see
# group_date_sharded_tables). A table that just happens to end in a
# date-shaped number shouldn't be swept into a "family" of one.
SCHEMA_SHARD_MIN_GROUP_SIZE = int(os.environ.get("SCHEMA_SHARD_MIN_GROUP_SIZE", 3))

# Matches <prefix>_<date> where date is YYYYMMDD, YYYYMM, YYYY-MM-DD, or
# YYYY_MM_DD - the common date-sharding conventions (BigQuery's own docs use
# YYYYMMDD; the others show up often enough in hand-rolled sharding to be
# worth covering). Prefix is matched non-greedily so a prefix that itself
# contains underscores (e.g. "raw_events_20240101" -> prefix "raw_events")
# still resolves to the longest non-date-shaped prefix via backtracking,
# not just the text before the first underscore.
_DATE_SHARD_RE = re.compile(
    r'^(?P<prefix>.+?)_(?P<date>\d{8}|\d{6}|\d{4}-\d{2}-\d{2}|\d{4}_\d{2}_\d{2})$'
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
        + "\n\n[... schema truncated: exceeded "
        + f"{max_chars:,} characters. Ask about fewer tables at once, or "
        + "reduce SCHEMA_MAX_CHARS/SCHEMA_MAX_TABLES scope on this "
        + "connection's dataset, to see more of it.]"
    )


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
        owns deciding what fallback text to show and whether to cache it."""

    @abstractmethod
    def execute(self, connection, sql_text):
        """Run one or more statements in `sql_text` and return a list of
        {"statement", "columns", "rows", "rowCount"} dicts, one per
        statement - the same shape execute_routes.py has always returned
        to the frontend. Let exceptions propagate; execute_routes.py is
        responsible for catching them and surfacing the raw error message
        to the client (see the docstring at the top of that file)."""

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