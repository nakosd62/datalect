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

from abc import ABC, abstractmethod


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