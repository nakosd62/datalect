"""
backends/__init__.py

Registry mapping a connection descriptor's "type" to a Backend instance.
db.py and execute_routes.py go through get_backend() rather than importing
a specific backend module directly - this is the one place that needs to
know the full set of supported dialects. Adding BigQuery/Snowflake/
Databricks later means adding one line here (and one new backend file),
not touching any route or db.py dispatch logic.
"""

from .base import Backend
from .postgres import PostgresBackend
from .bigquery import BigQueryBackend
from .snowflake import SnowflakeBackend
from .mysql import MySQLBackend
from .databricks import DatabricksBackend
from .oracle import OracleBackend
from .redshift import RedshiftBackend
from .mssql import MssqlBackend
from .sheets import SheetsBackend

_BACKENDS = {
    "postgres": PostgresBackend(),
    "bigquery": BigQueryBackend(),
    "snowflake": SnowflakeBackend(),
    "mysql": MySQLBackend(),
    "databricks": DatabricksBackend(),
    "oracle": OracleBackend(),
    "redshift": RedshiftBackend(),
    "mssql": MssqlBackend(),
    "sheets": SheetsBackend(),
}


def get_backend(descriptor):
    """Returns the Backend for a connection descriptor (a dict with a
    "type" key). Defaults to "postgres" for descriptors that don't specify
    a type - every descriptor built today (see db.py's _to_descriptor)
    predates multi-dialect support and is implicitly Postgres."""
    db_type = (descriptor or {}).get("type", "postgres")
    backend = _BACKENDS.get(db_type)
    if backend is None:
        raise ValueError(f"Unsupported database type: {db_type!r}")
    return backend


__all__ = ["Backend", "get_backend"]