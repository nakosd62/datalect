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
BigQuery connection - admin-configured presets (CONFIGURED_DBS, from
DATABASE_PRESETS) intentionally carry none, and instead authenticate as the
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

import decimal
import json

from google.cloud import bigquery
from google.oauth2 import service_account
import sqlparse

from .base import Backend


class BigQueryBackend(Backend):
    dialect_name = "BigQuery Standard SQL"

    def connect(self, descriptor):
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
        PostgresBackend.cache_key's username@dbname derivation."""
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

    def _run(self, connection, sql_text):
        """Runs one query/statement scoped to this connection's dataset
        (so unqualified table names in generated SQL resolve the same way
        Postgres's "public" schema does) and returns the finished job."""
        job_config = bigquery.QueryJobConfig(default_dataset=self._default_dataset_ref(connection))
        return connection.query(sql_text, job_config=job_config)

    def get_schema(self, connection):
        project_id = getattr(connection, "_ydyl_project_id", None)
        dataset = getattr(connection, "_ydyl_dataset", None)
        if not (project_id and dataset):
            return None

        qualified = f"`{project_id}.{dataset}`"
        schema_parts = []

        # 1. Tables and columns
        columns_rows = list(self._run(connection, f"""
            SELECT table_name, column_name, data_type, is_nullable
            FROM {qualified}.INFORMATION_SCHEMA.COLUMNS
            ORDER BY table_name, ordinal_position
        """).result())

        tables = {}
        for row in columns_rows:
            tables.setdefault(row.table_name, []).append(
                f"  {row.column_name} {row.data_type} "
                f"{'NULL' if row.is_nullable == 'YES' else 'NOT NULL'}"
            )
        for table_name, col_defs in tables.items():
            schema_parts.append(f"Table: {table_name}\n" + "\n".join(col_defs))

        # 2. Constraints - unenforced in BigQuery, but still useful context
        # for the model (e.g. which columns are meant to be primary/foreign
        # keys). Best-effort: TABLE_CONSTRAINTS/KEY_COLUMN_USAGE can 404 on
        # datasets with no declared constraints at all on some BigQuery
        # versions/regions, so a failure here just means "skip this
        # section", not "fail the whole schema fetch".
        try:
            constraint_rows = list(self._run(connection, f"""
                SELECT tc.table_name, tc.constraint_name, tc.constraint_type, kcu.column_name
                FROM {qualified}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                LEFT JOIN {qualified}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_name = kcu.table_name
                ORDER BY tc.table_name, tc.constraint_name
            """).result())
            if constraint_rows:
                lines = [
                    f"  [{r.table_name}] {r.constraint_name} ({r.constraint_type}): {r.column_name}"
                    for r in constraint_rows
                ]
                schema_parts.append("Constraints:\n" + "\n".join(lines))
        except Exception:
            pass

        # 3. Views
        try:
            view_rows = list(self._run(connection, f"""
                SELECT table_name, view_definition
                FROM {qualified}.INFORMATION_SCHEMA.VIEWS
            """).result())
            if view_rows:
                schema_parts.append(
                    "Views:\n" + "\n".join(
                        f"  View {r.table_name}: {(r.view_definition or '').strip()}"
                        for r in view_rows
                    )
                )
        except Exception:
            pass

        return "\n\n".join(schema_parts) if schema_parts else None

    def execute(self, connection, sql_text):
        statements = [s.strip() for s in sqlparse.split(sql_text) if s.strip()]
        results = []

        for stmt in statements:
            stmt_clean = stmt.rstrip(';').strip()
            if not stmt_clean:
                continue

            query_job = self._run(connection, stmt_clean)
            result = query_job.result()

            columns = None
            rows = None

            if result.schema:
                columns = [field.name for field in result.schema]
                rows = []
                for row in result:
                    row_dict = {}
                    for col in columns:
                        val = row[col]
                        if hasattr(val, 'isoformat'):
                            val = val.isoformat()
                        elif isinstance(val, decimal.Decimal):
                            val = float(val)
                        elif isinstance(val, bytes):
                            val = val.decode('utf-8', errors='replace')
                        row_dict[col] = val
                    rows.append(row_dict)
                count = len(rows)
            else:
                # DML (INSERT/UPDATE/DELETE/MERGE) or DDL - no result rows.
                affected = getattr(query_job, 'num_dml_affected_rows', None)
                count = affected if affected is not None else 0

            results.append({
                'statement': stmt_clean,
                'columns': columns,
                'rows': rows,
                'rowCount': count,
            })

        return results
