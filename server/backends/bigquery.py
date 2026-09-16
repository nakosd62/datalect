"""
backends/bigquery.py

BigQueryBackend: talks to Google BigQuery via google-cloud-bigquery. Mirrors
PostgresBackend's shape (see backends/postgres.py) so db.py/execute_routes.py/
config_routes.py never need to know which dialect they're actually talking
to - only the descriptor's "type" field decides that (see
backends/__init__.py's get_backend()).

A BigQuery descriptor looks like:
    {"type": "bigquery", "url": "bigquery://<project>/<dataset>",
     "project_id": "...", "dataset": "...", "credentials_json": "...",
     "billing_project_id": "..."}
"url"/"credentials_json"/"billing_project_id" are optional in different
contexts: "url" is a synthetic, non-secret identifier (see
config_routes.py's _bigquery_url) used for UI matching/display, never
something this backend itself needs to parse. "credentials_json" (a pasted
service-account key, JSON-encoded) is present only for a user's own custom
BigQuery connection - admin-configured presets (CONFIGURED_DBS, loaded from
DATABASE_PRESETS_FILE) intentionally carry none, and instead authenticate as the
app's own ambient identity (Application Default Credentials - the Cloud Run
service account in production, or whatever
`gcloud auth application-default login` set up locally).

"billing_project_id" is deliberately a separate concept from "project_id":
"project_id"/"dataset" say *where the data lives* (used for default_dataset
and INFORMATION_SCHEMA introspection below) - that can be any project the
active identity has read access to, including a project you don't own at
all, like Google's public datasets (bigquery-public-data). "billing_project_id"
says *whose quota pays for the query job* - almost always your own project.
Those two are the same project for an ordinary "query my own data" setup
(and this backend falls back to project_id when billing_project_id isn't
given, so that simple case needs nothing extra), but conflating them breaks
the moment project_id points at data you don't own: BigQuery would try to
bill the job to that project and get a 403 (typically "Access Denied: ...
does not have bigquery.jobs.create permission..."), since no ordinary
caller has job-creation rights on someone else's project. Callers building
descriptors (app_config.py for presets, config_routes.py for custom
connections) are responsible for populating billing_project_id sensibly -
see their comments for the actual defaulting rules.
"""

import json

from google.cloud import bigquery
from google.oauth2 import service_account
import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
    EXECUTE_RESULTS_MAX_ROWS, normalize_cell_value,
    find_naming_convention_relationships,
)


def _quote_ident(name):
    """Backtick-quotes a BigQuery column/identifier for interpolation into
    a plain SQL string. Every name this is called with is sourced from a
    prior catalog query (INFORMATION_SCHEMA.COLUMNS et al) rather than raw
    user input, matching this module's existing posture elsewhere (e.g. the
    project/dataset/table-name interpolation get_schema() already did
    before this change, for the wildcard shard-family form)."""
    return f"`{name}`"


# --- Phase 2 sampling/min-max column-type buckets ---------------------------
# Expressed in BigQuery Standard SQL's own INFORMATION_SCHEMA.COLUMNS.data_type
# spelling (unlike Postgres's information_schema, BigQuery's data_type is
# already the bare Standard SQL type name, e.g. "INT64"/"STRING", not a
# longer SQL-standard phrase) - narrower than "every conceivable type" on
# purpose: an ill-fitting MIN()/MAX() or frequent-value GROUP BY on the
# wrong shape of column is more likely to error or produce noise than a
# useful hint (mirrors backends/postgres.py's own NUMERIC_OR_DATE_TYPES/
# CATEGORICAL_TYPES sets).
NUMERIC_OR_DATE_TYPES = frozenset({
    "INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC", "DATE", "DATETIME", "TIMESTAMP", "TIME",
})
CATEGORICAL_TYPES = frozenset({"STRING", "BOOL"})

# Bounds on Phase 2's per-table sampling cost, all deliberately small - see
# get_schema()'s "Column value samples" section for how each is used.
# Mirrors backends/postgres.py's own constants of the same names/intent.
MAX_COLUMNS_FOR_SAMPLING = 25
MAX_NUMERIC_COLUMNS_FOR_MINMAX = 15
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE = 3
FREQUENT_VALUES_LIMIT = 15

# TABLESAMPLE SYSTEM percentage used for the frequent-value pass on each
# kept table (per docs/schema_context_recommendations.md's explicit
# BigQuery guidance) - bounds bytes scanned/shuffled for the GROUP BY
# regardless of table size. MIN()/MAX(), by contrast, run against the FULL
# table below rather than the sample: BigQuery bills/scans by column, so a
# MIN/MAX aggregate over one column costs proportionally to that column's
# own bytes (no shuffle, no grouping), not the whole table - sampling it
# would only trade an already-cheap, already-exact operation for imprecise
# bounds, which isn't a good trade the way it is for the GROUP BY.
FREQUENT_VALUES_SAMPLE_PERCENT = 10

# Cardinality gate for the frequent-value pass: a categorical column whose
# live APPROX_COUNT_DISTINCT() is at/above this fraction of the table's own
# live row count is treated as "near-unique" (e.g. a UUID/token column) -
# not worth a frequent-value sample. Mirrors backends/postgres.py's
# pg_stats.n_distinct-based gate, just computed live here (one cheap
# APPROX_COUNT_DISTINCT() query, not an exact COUNT(DISTINCT ...)) since
# BigQuery has no free planner-stats table to read this from instead.
NEAR_UNIQUE_APPROX_DISTINCT_RATIO = 0.9


def _is_near_unique_approx_distinct(distinct_n, live_row_count):
    """See NEAR_UNIQUE_APPROX_DISTINCT_RATIO above. None/unknown counts as
    "not near-unique" (render the sample) rather than silently hiding it -
    a failed/skipped APPROX_COUNT_DISTINCT() shouldn't also suppress an
    otherwise-working frequent-value query for that column."""
    if distinct_n is None or not live_row_count or live_row_count <= 0:
        return False
    return (distinct_n / live_row_count) >= NEAR_UNIQUE_APPROX_DISTINCT_RATIO


def _bigquery_partition_filter_clause(column_name, data_type):
    """Best-effort WHERE-clause fragment satisfying a
    require_partition_filter=true table's mandatory partition-column
    predicate, for Phase 2's own sampling/live-count/min-max queries below
    (see get_schema()'s per-table Phase 2 loop) - without this, every one
    of those new queries would itself fail against exactly the tables this
    flag exists to protect, with an error like "Cannot query over table
    '...' without a filter over column(s) '...' that can be used for
    partition elimination".

    Time-based partitioning (a DATE/DATETIME/TIMESTAMP partitioning column
    - the overwhelming majority of real BigQuery partitioned tables) gets a
    genuine, safely-satisfiable recent-window predicate. A partitioning
    column typed some other way (integer-range partitioning, or anything
    this wasn't written against) falls back to an "IS NOT NULL" predicate,
    which is best-effort only - flagged here, not silently assumed correct,
    since it may not actually satisfy BigQuery's partition-elimination
    requirement for a non-time partitioning scheme; this sandbox has no
    live BigQuery project to verify that against (see the module docstring
    for the general shape of that constraint)."""
    ident = _quote_ident(column_name)
    if data_type == "DATE":
        return f"{ident} >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)"
    if data_type == "DATETIME":
        return f"{ident} >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 30 DAY)"
    if data_type == "TIMESTAMP":
        return f"{ident} >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
    return f"{ident} IS NOT NULL"


class BigQueryBackend(Backend):
    dialect_name = "BigQuery Standard SQL"

    def connect(self, descriptor):
        # No connect-only timeout kwarg here - see backends/base.py's
        # DB_CONNECT_TIMEOUT_SECONDS docstring for why BigQuery doesn't need
        # one (bigquery.Client() construction below doesn't dial out
        # synchronously the way a real TCP connect() does). A descriptor's
        # own per-dataset "connect_timeout_seconds" override therefore has
        # nothing to attach to and is silently a no-op for this dialect -
        # its "execute_timeout_seconds" counterpart still works normally,
        # enforced generically by execute_routes.py's _execute_with_timeout
        # around the whole execute() call (job submission + polling), not
        # via any client-construction kwarg here.
        project_id = (descriptor or {}).get("project_id") or ""
        dataset = (descriptor or {}).get("dataset") or ""
        credentials_json = (descriptor or {}).get("credentials_json")
        billing_project_id = (descriptor or {}).get("billing_project_id") or ""

        credentials = None
        if credentials_json:
            info = json.loads(credentials_json)
            credentials = service_account.Credentials.from_service_account_info(info)
            # A pasted key's own project (where it was minted) is a
            # reasonable *data-location* default too, absent an explicit
            # project_id - matches how a user pastes a key for "their"
            # project without necessarily also retyping the project id.
            # Billing still defaults separately below.
            project_id = project_id or info.get("project_id", "")

        # The project actually charged/executed against - see the class
        # docstring for why this must NOT just be project_id. Falls back to
        # project_id only when no caller supplied a billing_project_id at
        # all, which keeps the common "querying my own project" case
        # working with zero extra config.
        client = bigquery.Client(project=(billing_project_id or project_id or None), credentials=credentials)
        # Stashed so identity_label()/get_schema()/execute() - which only
        # receive the live client back, not the original descriptor - can
        # still scope queries to the right dataset and report it.
        client._ydyl_project_id = project_id
        client._ydyl_dataset = dataset
        return client

    def close(self, connection):
        if connection is not None and hasattr(connection, "close"):
            connection.close()

    def cache_key(self, descriptor):
        """project.dataset, parsed straight from the descriptor - never a
        credential. Same non-sensitive-identifier role db.py's
        get_conn_identifier has always played, mirrored from
        PostgresBackend.cache_key's username@host:port/dbname derivation."""
        project_id = (descriptor or {}).get("project_id") or "unknown"
        dataset = (descriptor or {}).get("dataset") or "unknown"
        return f"{project_id}.{dataset}"

    def identity_label(self, connection):
        """BigQuery has no single "current database/user" query the way
        Postgres does - project+dataset is the closest equivalent, and it's
        already known (not worth a round-trip) since connect() stashed it."""
        project_id = getattr(connection, "_ydyl_project_id", None) or "Unknown"
        dataset = getattr(connection, "_ydyl_dataset", None) or "Unknown"
        return dataset, project_id

    def _default_dataset_ref(self, connection):
        project_id = getattr(connection, "_ydyl_project_id", None)
        dataset = getattr(connection, "_ydyl_dataset", None)
        if project_id and dataset:
            return bigquery.DatasetReference(project_id, dataset)
        return None

    def _run(self, connection, sql_text, params=None):
        """Runs one query/statement scoped to this connection's dataset
        (so unqualified table names in generated SQL resolve the same way
        Postgres's "public" schema does) and returns the finished job.
        `params`, when given, is a list of bigquery.ScalarQueryParameter/
        ArrayQueryParameter objects for a parameterized query - used by
        get_schema() below to scope INFORMATION_SCHEMA queries to a bounded
        table-name set without string-formatting names into the SQL."""
        job_config = bigquery.QueryJobConfig(
            default_dataset=self._default_dataset_ref(connection),
            query_parameters=params or [],
        )
        return connection.query(sql_text, job_config=job_config)

    def _build_shallow_schema_parts(self, connection):
        """Phase 1 (catalog-only, no live queries): every query both
        get_schema_shallow() and get_schema() (deep) need, run exactly once
        here and shared by both - see backends/base.py's Backend.get_schema()/
        get_schema_shallow() docstrings for why this split exists at all,
        and backends/postgres.py's own _build_shallow_schema_parts for the
        structural pattern this mirrors (BigQuery's own optional-section
        try/except style is unchanged from before this split - see each
        section below).

        Returns None if the dataset has no BASE TABLE/EXTERNAL table at all
        (mirrors the old get_schema()'s "return None" for that case).
        Otherwise returns (schema_parts, table_columns, phase2_ctx):
          - schema_parts: the ordered list of text sections, not yet joined/
            capped - identical in kind to what get_schema() used to build
            directly, just returned before the final cap_schema_text() call.
          - table_columns: {table_name: [column_name, ...]}, scoped to the
            same bounded kept_names set schema_parts describes - handed to
            the shared find_naming_convention_relationships() helper by
            get_schema()'s Phase 2 pass (no extra query needed for that).
          - phase2_ctx: raw, already-fetched data Phase 2 wants to reuse
            without re-querying - kept_names/column_types (for deciding
            what to sample), shard_by_representative and the partition-
            filter/partitioning-column facts (so Phase 2's own new queries
            can thread a required partition filter through - see
            get_schema()'s per-table loop), and the raw view/routine rows
            (so get_schema() can render their full body text without a
            second trip to the database; see the "Views"/"Routines"
            sections below for why only the *name*/*signature* is rendered
            here).
        """
        project_id = getattr(connection, "_ydyl_project_id", None)
        dataset = getattr(connection, "_ydyl_dataset", None)
        if not (project_id and dataset):
            return None

        qualified = f"`{project_id}.{dataset}`"
        schema_parts = []
        table_columns = {}
        column_types = {}
        partitioning_columns = {}

        # Phase 1: cheap - just the distinct table names, bounded so a
        # dataset with an extreme number of tables can't make even this
        # scan unbounded (SCHEMA_MAX_TABLE_NAMES_SCANNED). Grouped into
        # date-shard families (e.g. events_20240101 .. events_20241231 ->
        # one "events" family) and capped to SCHEMA_MAX_TABLES entries (see
        # backends/base.py) *before* any column/constraint query runs -
        # those get scoped to this bounded set below, which is what
        # actually keeps schema fetching tractable on a dataset with a huge
        # number of tables, rather than fetching everything and truncating
        # the text after the fact. BASE TABLE and EXTERNAL only (matches
        # postgres.py's own BASE-TABLE-only scoping, widened here to also
        # surface external/federated tables per the external-table flag
        # below) - views are handled separately, unscoped, in their own
        # section further down.
        table_rows = list(self._run(connection, f"""
            SELECT table_name, table_type
            FROM {qualified}.INFORMATION_SCHEMA.TABLES
            WHERE table_type IN ('BASE TABLE', 'EXTERNAL')
            ORDER BY table_name
            LIMIT {int(SCHEMA_MAX_TABLE_NAMES_SCANNED)}
        """).result())
        all_table_names = [row.table_name for row in table_rows]
        table_types = {row.table_name: getattr(row, "table_type", "BASE TABLE") for row in table_rows}

        if not all_table_names:
            return None

        kept_names, shard_groups = group_date_sharded_tables(all_table_names)
        kept_names, shard_groups, omitted_count = cap_kept_tables(kept_names, shard_groups)
        # Unlike Postgres (no wildcard-table mechanism - see
        # backends/postgres.py's shard_by_representative comment), BigQuery
        # has a native syntax for querying a whole date-shard family at
        # once: `project.dataset.prefix_*` plus the pseudo-column
        # _TABLE_SUFFIX to filter/identify which shard each row came from.
        # So a shard family's schema entry here describes that wildcard
        # form directly - correctness-preserving, not just an explanatory
        # note the way Postgres's representative-table approach is.
        shard_by_representative = {
            members[-1]: (prefix, members) for prefix, members in shard_groups.items()
        }

        kept_names_param = bigquery.ArrayQueryParameter("kept_names", "STRING", kept_names)

        # 1. Tables and columns - scoped to the bounded kept_names set.
        # is_partitioning_column/clustering_ordinal_position (new) are a
        # free addition to this same query (per
        # docs/schema_context_recommendations.md's explicit note that this
        # is "a free column addition, not a new query") rather than a
        # second query - both already live on INFORMATION_SCHEMA.COLUMNS.
        columns_rows = list(self._run(connection, f"""
            SELECT table_name, column_name, data_type, is_nullable,
                   is_partitioning_column, clustering_ordinal_position
            FROM {qualified}.INFORMATION_SCHEMA.COLUMNS
            WHERE table_name IN UNNEST(@kept_names)
            ORDER BY table_name, ordinal_position
        """, params=[kept_names_param]).result())

        tables = {}
        clustering_positions = {}
        for row in columns_rows:
            table_name = row.table_name
            column_name = row.column_name
            data_type = row.data_type
            # getattr(..., default) rather than direct attribute access for
            # the two new fields: a real BigQuery row always has them (this
            # query always selects them), but tolerates a test fake that
            # doesn't bother supplying every new field for a scenario that
            # isn't testing them.
            is_partitioning_column = getattr(row, "is_partitioning_column", "NO")
            clustering_ordinal_position = getattr(row, "clustering_ordinal_position", None)

            table_columns.setdefault(table_name, []).append(column_name)
            column_types.setdefault(table_name, {})[column_name] = data_type

            annotations = []
            if is_partitioning_column == "YES":
                annotations.append("PARTITION")
                partitioning_columns.setdefault(table_name, column_name)
            if clustering_ordinal_position is not None:
                annotations.append(f"CLUSTER #{int(clustering_ordinal_position)}")
                clustering_positions.setdefault(table_name, []).append(
                    (int(clustering_ordinal_position), column_name)
                )
            annotation_str = f" [{', '.join(annotations)}]" if annotations else ""

            tables.setdefault(table_name, []).append(
                f"  {column_name} {data_type} "
                f"{'NULL' if row.is_nullable == 'YES' else 'NOT NULL'}{annotation_str}"
            )

        # 2. Table-level options (new): comments (description) and the
        # correctness-gating require_partition_filter flag, both from the
        # same INFORMATION_SCHEMA.TABLE_OPTIONS family - one combined query
        # rather than two, since both are simple option_name/option_value
        # rows from the exact same view. Best-effort/try-except like every
        # optional section here, EXCEPT that a require_partition_filter=true
        # result is still rendered inline on the table's own heading below
        # (not just in a silent/best-effort side section) - see the plan
        # this implements: that flag directly affects whether generated SQL
        # will even run, so it must always be visible when true, unlike the
        # comments half of this same query.
        table_comments = {}
        require_partition_filter_flags = {}
        try:
            option_rows = list(self._run(connection, f"""
                SELECT table_name, option_name, option_value
                FROM {qualified}.INFORMATION_SCHEMA.TABLE_OPTIONS
                WHERE table_name IN UNNEST(@kept_names)
                  AND option_name IN ('description', 'require_partition_filter')
            """, params=[kept_names_param]).result())
            for r in option_rows:
                option_name = getattr(r, "option_name", None)
                raw_value = getattr(r, "option_value", None)
                if option_name == "description":
                    # BigQuery renders a STRING option's value pre-quoted,
                    # as it would appear in DDL (e.g. '"my description"') -
                    # strip the surrounding quotes so the rendered comment
                    # text is plain.
                    text = raw_value
                    if isinstance(text, str) and len(text) >= 2 and text[0] == '"' and text[-1] == '"':
                        text = text[1:-1]
                    if text:
                        table_comments[r.table_name] = text
                elif option_name == "require_partition_filter":
                    require_partition_filter_flags[r.table_name] = (
                        str(raw_value).strip().lower() == "true"
                    )
        except Exception:
            pass

        for table_name in kept_names:
            col_defs = tables.get(table_name)
            if not col_defs:
                continue
            if table_name in shard_by_representative:
                prefix, members = shard_by_representative[table_name]
                wildcard = f"`{project_id}.{dataset}.{prefix}_*`"
                heading = (
                    f"Table family: {wildcard} ({len(members)} date-sharded tables, "
                    f"e.g. {members[0]} .. {members[-1]}; identical columns in every "
                    f"member - query this family with the wildcard form "
                    f"{wildcard}, filtering/identifying the shard via the "
                    f"_TABLE_SUFFIX pseudo-column (e.g. WHERE _TABLE_SUFFIX "
                    f"BETWEEN '...' AND '...'); never query a single literal "
                    f"date-suffixed table name from this family)"
                )
            else:
                heading = f"Table: {table_name}"
            heading_annotations = []
            if table_types.get(table_name) == "EXTERNAL":
                heading_annotations.append("external table")
            if require_partition_filter_flags.get(table_name):
                part_col = partitioning_columns.get(table_name)
                heading_annotations.append(
                    f"REQUIRES PARTITION FILTER on {part_col}" if part_col
                    else "REQUIRES PARTITION FILTER"
                )
            if heading_annotations:
                heading += f" [{'; '.join(heading_annotations)}]"
            schema_parts.append(heading + "\n" + "\n".join(col_defs))

        if omitted_count:
            schema_parts.append(
                f"[... {omitted_count} more table(s)/table-family(ies) not shown - "
                f"this dataset has more than the {SCHEMA_MAX_TABLES}-table summary "
                f"limit. Ask about a narrower set of tables to see the rest.]"
            )

        if table_comments:
            schema_parts.append(
                "Table comments:\n" + "\n".join(
                    f"  {t}: {c}" for t, c in sorted(table_comments.items())
                )
            )

        # 3. Constraints - unenforced in BigQuery, but still useful context
        # for the model (e.g. which columns are meant to be primary/foreign
        # keys). Best-effort: TABLE_CONSTRAINTS/KEY_COLUMN_USAGE can 404 on
        # datasets with no declared constraints at all on some BigQuery
        # versions/regions, so a failure here just means "skip this
        # section", not "fail the whole schema fetch". Scoped to
        # kept_names, same as section 1.
        try:
            constraint_rows = list(self._run(connection, f"""
                SELECT tc.table_name, tc.constraint_name, tc.constraint_type, kcu.column_name
                FROM {qualified}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                LEFT JOIN {qualified}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_name = kcu.table_name
                WHERE tc.table_name IN UNNEST(@kept_names)
                ORDER BY tc.table_name, tc.constraint_name
            """, params=[kept_names_param]).result())
            if constraint_rows:
                lines = [
                    f"  [{r.table_name}] {r.constraint_name} ({r.constraint_type}): {r.column_name}"
                    for r in constraint_rows
                ]
                schema_parts.append("Constraints:\n" + "\n".join(lines))
        except Exception:
            pass

        # 4. Views - deliberately NOT scoped to kept_names: that set is
        # built exclusively from BASE TABLE/EXTERNAL names (phase 1 filters
        # table_type IN ('BASE TABLE', 'EXTERNAL')), so no view name could
        # ever appear in it - scoping this query to kept_names would
        # silently return zero views, always. Views are a categorically
        # separate set and aren't subject to the same table-count blowup
        # this whole cap/collapse scheme protects against, so leaving this
        # unbounded is intentional, not an oversight (mirrors
        # backends/postgres.py).
        #
        # Shallow rendering is name-only (no view_definition body) - the
        # raw rows (including each view's body) are still fetched here (one
        # query, reused by both phases) and threaded through via
        # phase2_ctx below so get_schema() (deep) can render the full body
        # without a second query - see get_schema()'s "View definitions"
        # section.
        view_rows = []
        try:
            view_rows = list(self._run(connection, f"""
                SELECT table_name, view_definition
                FROM {qualified}.INFORMATION_SCHEMA.VIEWS
            """).result())
            if view_rows:
                schema_parts.append(
                    "Views:\n" + "\n".join(f"  View {r.table_name}" for r in view_rows)
                )
        except Exception:
            pass

        # 5. Row-count estimate (new) - INFORMATION_SCHEMA.TABLE_STORAGE.
        # total_rows, a dataset-level view flagged in the plan as
        # best-effort: it can be slower/less available than TABLES/COLUMNS
        # in some BigQuery configurations, so a failure here must not break
        # the rest of the fetch (same posture as every other try/except
        # section, just called out explicitly since this one's latency
        # profile is the least certain of the bunch).
        try:
            storage_rows = list(self._run(connection, f"""
                SELECT table_name, total_rows
                FROM {qualified}.INFORMATION_SCHEMA.TABLE_STORAGE
                WHERE table_name IN UNNEST(@kept_names)
            """, params=[kept_names_param]).result())
            estimate_lines = [
                f"  {r.table_name}: ~{r.total_rows} rows (estimate)"
                for r in storage_rows if getattr(r, "total_rows", None) is not None
            ]
            if estimate_lines:
                schema_parts.append("Row count estimates:\n" + "\n".join(estimate_lines))
        except Exception:
            pass

        # 6. Routines (new) - existence + signature only, no body (see
        # get_schema()'s "Routine definitions" section for the full-body
        # deep-only counterpart, reusing routine_definition fetched here
        # rather than re-querying it). Two queries (ROUTINES, then
        # PARAMETERS joined in Python by specific_name) rather than one
        # aggregating join, so a test/mocked harness - and a real BigQuery
        # dataset with no PARAMETERS rows for a given routine - both degrade
        # cleanly to "name + return type, empty signature" instead of a
        # more fragile combined query. Not scoped to kept_names (like Views
        # above) - routines aren't tables.
        routines_data = []
        try:
            routine_rows = list(self._run(connection, f"""
                SELECT routine_name, specific_name, data_type, routine_definition
                FROM {qualified}.INFORMATION_SCHEMA.ROUTINES
                ORDER BY routine_name
            """).result())
            params_by_specific = {}
            try:
                param_rows = list(self._run(connection, f"""
                    SELECT specific_name, parameter_name, data_type, ordinal_position
                    FROM {qualified}.INFORMATION_SCHEMA.PARAMETERS
                    ORDER BY specific_name, ordinal_position
                """).result())
                for p in param_rows:
                    params_by_specific.setdefault(p.specific_name, []).append(
                        f"{p.parameter_name} {p.data_type}"
                    )
            except Exception:
                pass

            routine_lines = []
            for r in routine_rows:
                signature = ", ".join(params_by_specific.get(r.specific_name, []))
                return_type = getattr(r, "data_type", None) or ""
                routines_data.append({
                    "name": r.routine_name,
                    "signature": signature,
                    "return_type": return_type,
                    "definition": getattr(r, "routine_definition", None),
                })
                routine_lines.append(f"  {r.routine_name}({signature}) -> {return_type}")
            if routine_lines:
                schema_parts.append("Routines:\n" + "\n".join(routine_lines))
        except Exception:
            pass

        # Session facts: skipped entirely, unlike Postgres/etc - BigQuery
        # has no session/connection concept in the usual sense (each query
        # is stateless from the client's perspective, aside from the
        # default_dataset this backend's own _run() already sets), so
        # there's no real "session timezone"/"default collation" fact to
        # report here.
        #
        # Grants: skipped entirely - BigQuery's IAM-based permission model
        # doesn't map cleanly onto a simple current-user table-grants view
        # the way SQL-standard information_schema.role_table_grants does
        # for Postgres/MySQL, so this is left out rather than forced into
        # an ill-fitting shape (per the plan: "skip unless a clearly safe
        # catalog source turns up").
        #
        # Column-level comments: skipped - INFORMATION_SCHEMA.COLUMN_FIELD_
        # PATHS.description is flagged in the recommendations doc as "less
        # consistently exposed" than the table-level TABLE_OPTIONS source
        # above, and this sandbox has no live BigQuery project to confirm
        # its exact queryable shape, so table-level comments alone are
        # shipped rather than guessing at a shakier column-level source.
        #
        # Column-level security (policy tags): skipped - not reliably
        # introspectable per the recommendations doc.

        phase2_ctx = {
            "kept_names": kept_names,
            "column_types": column_types,
            "shard_by_representative": shard_by_representative,
            "require_partition_filter": require_partition_filter_flags,
            "partitioning_columns": partitioning_columns,
            "views": view_rows,
            "routines": routines_data,
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

        project_id = getattr(connection, "_ydyl_project_id", None)
        dataset = getattr(connection, "_ydyl_dataset", None)
        kept_names = phase2_ctx["kept_names"]
        column_types = phase2_ctx["column_types"]
        shard_by_representative = phase2_ctx["shard_by_representative"]
        require_partition_filter = phase2_ctx["require_partition_filter"]
        partitioning_columns = phase2_ctx["partitioning_columns"]
        views = phase2_ctx["views"]
        routines = phase2_ctx["routines"]

        # Phase 2 (deep-only): full view/routine bodies, reusing the raw
        # rows _build_shallow_schema_parts already fetched - no re-query.
        if views:
            view_lines = [
                f"  View {r.table_name}: {(r.view_definition or '').strip()}"
                for r in views if (r.view_definition or "").strip()
            ]
            if view_lines:
                schema_parts.append("View definitions:\n" + "\n".join(view_lines))

        routine_body_lines = [
            f"  {r['name']}: {(r['definition'] or '').strip()}"
            for r in routines if (r.get("definition") or "").strip()
        ]
        if routine_body_lines:
            schema_parts.append("Routine definitions:\n" + "\n".join(routine_body_lines))

        # Phase 2 (deep-only, live queries): frequent values/min-max,
        # cardinality gating, live row counts - per kept table. Real
        # BigQuery cost/billing implications (unlike the free
        # INFORMATION_SCHEMA metadata queries above), so this whole block
        # is reachable only from get_schema() (deep), never from
        # get_schema_shallow().
        live_count_lines = []
        sample_blocks = []
        for table_name in kept_names:
            col_types = column_types.get(table_name) or {}
            if not col_types:
                # A kept_names entry the columns query returned nothing
                # for (shouldn't normally happen - kept_names and
                # column_types are built from the same query - but guards
                # against an empty/omitted table cleanly).
                continue

            if table_name in shard_by_representative:
                prefix, _members = shard_by_representative[table_name]
                query_target = f"`{project_id}.{dataset}.{prefix}_*`"
            else:
                query_target = f"`{project_id}.{dataset}.{table_name}`"

            # If Phase 1 flagged this table as require_partition_filter,
            # every query below MUST include a partition filter itself -
            # otherwise these new Phase 2 queries would fail against
            # exactly the tables this flag exists to protect (see the
            # plan this implements and _bigquery_partition_filter_clause's
            # own docstring). When the flag is set but Phase 1 couldn't
            # identify which column is the partitioning column, skip this
            # table's Phase 2 queries entirely rather than risk sending an
            # unfiltered query BigQuery will reject.
            partition_where = ""
            if require_partition_filter.get(table_name):
                part_col = partitioning_columns.get(table_name)
                if not part_col:
                    continue
                clause = _bigquery_partition_filter_clause(part_col, col_types.get(part_col))
                partition_where = f" WHERE {clause}"

            # Live row count - authoritative, unlike Phase 1's
            # TABLE_STORAGE estimate (free, but can lag actual state). Real
            # cost implication (a full-table scan/count), unlike every
            # Phase 1 INFORMATION_SCHEMA query above.
            live_row_count = None
            try:
                count_rows = list(self._run(
                    connection, f"SELECT COUNT(*) AS n FROM {query_target}{partition_where}"
                ).result())
                if count_rows:
                    live_row_count = count_rows[0].n
                    live_count_lines.append(f"  {table_name}: {live_row_count} rows (live, authoritative)")
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
            # against the FULL table (not a sample) - see
            # FREQUENT_VALUES_SAMPLE_PERCENT's docstring for why that's a
            # deliberate, cheaper choice than TABLESAMPLE-ing this one too.
            # Positional aliases (min_0/max_0/...) rather than
            # name-derived ones, so a column name needing its own quoting
            # can never collide with the alias syntax.
            if numeric_cols:
                try:
                    select_parts = ", ".join(
                        f"MIN({_quote_ident(c)}) AS min_{i}, MAX({_quote_ident(c)}) AS max_{i}"
                        for i, c in enumerate(numeric_cols)
                    )
                    minmax_rows = list(self._run(
                        connection, f"SELECT {select_parts} FROM {query_target}{partition_where}"
                    ).result())
                    if minmax_rows:
                        row = minmax_rows[0]
                        for i, c in enumerate(numeric_cols):
                            min_v = getattr(row, f"min_{i}", None)
                            max_v = getattr(row, f"max_{i}", None)
                            table_sample_lines.append(f"    {c}: range [{min_v} .. {max_v}]")
                except Exception:
                    pass

            # Cardinality gate via APPROX_COUNT_DISTINCT - cheap relative
            # to an exact COUNT(DISTINCT ...) - compared against this
            # table's live row count (already fetched above) to decide
            # "near-unique, frequent-value sampling isn't a useful hint"
            # (see _is_near_unique_approx_distinct).
            eligible_categorical = []
            if categorical_cols:
                approx_distinct = {}
                try:
                    select_parts = ", ".join(
                        f"APPROX_COUNT_DISTINCT({_quote_ident(c)}) AS distinct_{i}"
                        for i, c in enumerate(categorical_cols)
                    )
                    distinct_rows = list(self._run(
                        connection, f"SELECT {select_parts} FROM {query_target}{partition_where}"
                    ).result())
                    if distinct_rows:
                        row = distinct_rows[0]
                        for i, c in enumerate(categorical_cols):
                            approx_distinct[c] = getattr(row, f"distinct_{i}", None)
                except Exception:
                    pass

                for c in categorical_cols:
                    if _is_near_unique_approx_distinct(approx_distinct.get(c), live_row_count):
                        continue
                    eligible_categorical.append(c)
                    if len(eligible_categorical) >= MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE:
                        break

            # Frequent values - one query per eligible categorical column
            # (capped at MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE per
            # table), against a TABLESAMPLE SYSTEM slice rather than the
            # full table (per the recommendations doc's explicit BigQuery
            # guidance - see FREQUENT_VALUES_SAMPLE_PERCENT's docstring).
            # Wrapped in the same try/except as every other optional
            # section here - notably, TABLESAMPLE against a wildcard
            # shard-family reference is unverified from this sandbox and
            # may not be supported by BigQuery at all, so a failure here
            # degrades to "skip this table's frequent-value section",
            # never the rest of the fetch.
            for c in eligible_categorical:
                try:
                    freq_rows = list(self._run(connection, f"""
                        SELECT {_quote_ident(c)} AS val, COUNT(*) AS cnt
                        FROM {query_target} TABLESAMPLE SYSTEM ({FREQUENT_VALUES_SAMPLE_PERCENT} PERCENT)
                        {partition_where}
                        GROUP BY val
                        ORDER BY cnt DESC
                        LIMIT {FREQUENT_VALUES_LIMIT}
                    """).result())
                    if freq_rows:
                        freq_text = ", ".join(f"{r.val} ({r.cnt})" for r in freq_rows)
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
        statements = [s.strip() for s in sqlparse.split(sql_text) if s.strip()]
        results = []

        for stmt in statements:
            stmt_clean = stmt.rstrip(';').strip()
            if not stmt_clean:
                continue

            try:
                query_job = self._run(connection, stmt_clean)
                # max_results here is BigQuery's own, API-level equivalent
                # of the other backends' cursor.fetchmany(EXECUTE_RESULTS_MAX_ROWS)
                # (see backends/base.py's EXECUTE_RESULTS_MAX_ROWS docstring)
                # - the RowIterator this returns simply never yields more
                # than that many rows, and never asks the API for more
                # pages than it takes to produce them, so a query matching
                # millions of rows never gets iterated in full here either.
                result = query_job.result(max_results=EXECUTE_RESULTS_MAX_ROWS)

                columns = None
                rows = None
                truncated = False

                if result.schema:
                    columns = [field.name for field in result.schema]
                    rows = []
                    for row in result:
                        rows.append({col: normalize_cell_value(row[col]) for col in columns})
                    count = len(rows)
                    # RowIterator.total_rows reflects the query's REAL total
                    # result size regardless of max_results above (it's
                    # populated from the job's own metadata, not by
                    # counting what this iterator actually yielded) - so
                    # this is a real truncation check, not a guess from
                    # count == the cap.
                    total_rows = getattr(result, 'total_rows', None)
                    truncated = total_rows is not None and total_rows > count
                else:
                    # DML (INSERT/UPDATE/DELETE/MERGE) or DDL - no result rows.
                    affected = getattr(query_job, 'num_dml_affected_rows', None)
                    count = affected if affected is not None else 0

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