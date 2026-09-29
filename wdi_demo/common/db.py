"""
Database connection, schema (re)creation, size-budget checks, and bulk
insert helpers. Written to work identically against Postgres and
MySQL/MariaDB (including Aiven-hosted instances of either) via SQLAlchemy.
"""

import os
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from common.schema import FACT_TABLES, TABLE_LOAD_ORDER, metadata


def _normalize_url(raw_url: str) -> str:
    """Accept the connection string exactly as Aiven displays it (e.g.
    ``postgres://...`` or ``mysql://...``) and rewrite the scheme to the
    driver SQLAlchemy should use, without touching anything else (host,
    port, credentials, query string all pass through untouched)."""
    if raw_url.startswith("postgres://"):
        return "postgresql+psycopg2://" + raw_url[len("postgres://"):]
    if raw_url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + raw_url[len("postgresql://"):]
    if raw_url.startswith("mysql://"):
        return "mysql+pymysql://" + raw_url[len("mysql://"):]
    return raw_url


def build_engine() -> Engine:
    """Build a SQLAlchemy engine from environment variables.

    Two ways to configure it (see .env.example):

    1. DATABASE_URL - a single connection string, exactly as Aiven's
       console shows it for the "Service URI" (postgres:// or mysql://).
    2. Discrete DB_ENGINE / DB_HOST / DB_PORT / DB_USER / DB_PASSWORD /
       DB_NAME / DB_SSLMODE / DB_SSL_CA_PATH variables, for when you'd
       rather copy individual fields out of the Aiven console.
    """
    raw_url = os.environ.get("DATABASE_URL")
    connect_args = {}

    if raw_url:
        url = make_url(_normalize_url(raw_url))
    else:
        engine_name = os.environ.get("DB_ENGINE", "").lower()
        if engine_name not in ("postgres", "postgresql", "mysql"):
            sys.exit(
                "Set DATABASE_URL, or DB_ENGINE=postgres|mysql plus the discrete "
                "DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME variables. See .env.example."
            )
        host = os.environ["DB_HOST"]
        port = os.environ.get("DB_PORT", "5432" if engine_name.startswith("postgres") else "3306")
        user = os.environ["DB_USER"]
        password = os.environ["DB_PASSWORD"]
        dbname = os.environ["DB_NAME"]
        driver = "postgresql+psycopg2" if engine_name.startswith("postgres") else "mysql+pymysql"
        url = make_url(f"{driver}://{user}:{password}@{host}:{port}/{dbname}")

    ssl_ca_path = os.environ.get("DB_SSL_CA_PATH")
    sslmode = os.environ.get("DB_SSLMODE", "require")

    if url.get_backend_name() == "postgresql":
        # psycopg2 accepts sslmode / sslrootcert as libpq connect args.
        connect_args["sslmode"] = sslmode
        if ssl_ca_path:
            connect_args["sslrootcert"] = ssl_ca_path
    elif url.get_backend_name() == "mysql":
        if ssl_ca_path:
            connect_args["ssl"] = {"ca": ssl_ca_path}
        elif sslmode != "disable":
            # PyMySQL: passing an empty ssl dict still enables TLS on the
            # wire without strict CA verification, which matches Aiven's
            # "require" behaviour for Postgres above. Good enough as a
            # default; pass DB_SSL_CA_PATH for verified TLS.
            connect_args["ssl"] = {}

    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


def wait_for_connection(engine: Engine, attempts: int = 5, delay_seconds: float = 3.0) -> None:
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001 - we re-raise after logging
            last_err = exc
            print(f"  connection attempt {attempt}/{attempts} failed: {exc}", file=sys.stderr)
            time.sleep(delay_seconds)
    raise SystemExit(f"Could not connect to the database after {attempts} attempts: {last_err}")


def create_schema(engine: Engine, drop_first: bool = False) -> None:
    if drop_first:
        metadata.drop_all(engine)
    metadata.create_all(engine)


def bulk_insert(engine: Engine, table, rows, chunk_size: int) -> int:
    """Insert `rows` (list of dicts) into `table` in chunks, skipping rows
    that already exist (ON CONFLICT / INSERT IGNORE semantics) so re-running
    the script is safe. Returns the number of rows inserted."""
    if not rows:
        return 0

    dialect = engine.dialect.name
    total = 0
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start:start + chunk_size]
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                stmt = pg_insert(table).values(chunk)
                stmt = stmt.on_conflict_do_nothing()
                conn.execute(stmt)
            elif dialect == "mysql":
                from sqlalchemy.dialects.mysql import insert as mysql_insert
                stmt = mysql_insert(table).values(chunk)
                # INSERT IGNORE-equivalent: on duplicate key, keep existing row.
                first_col = list(table.c.keys())[0]
                stmt = stmt.on_duplicate_key_update({first_col: stmt.inserted[first_col]})
                conn.execute(stmt)
            else:
                conn.execute(table.insert(), chunk)
            total += len(chunk)
    return total


def measured_table_sizes(engine: Engine) -> dict:
    """Return {table_name: bytes_on_disk} using each engine's own catalog,
    i.e. ground truth rather than an estimate."""
    sizes = {}
    dialect = engine.dialect.name
    table_names = [t.name for t in TABLE_LOAD_ORDER]

    with engine.connect() as conn:
        if dialect == "postgresql":
            for name in table_names:
                result = conn.execute(
                    text("SELECT pg_total_relation_size(:name)"),
                    {"name": name},
                ).scalar()
                sizes[name] = int(result or 0)
        elif dialect == "mysql":
            rows = conn.execute(
                text(
                    "SELECT table_name, data_length + index_length AS total_bytes "
                    "FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
            )
            for row in rows:
                if row[0] in table_names:
                    sizes[row[0]] = int(row[1] or 0)
        else:
            for name in table_names:
                sizes[name] = 0
    return sizes


def row_counts(engine: Engine) -> dict:
    counts = {}
    with engine.connect() as conn:
        for table in TABLE_LOAD_ORDER:
            counts[table.name] = conn.execute(
                text(f"SELECT COUNT(*) FROM {table.name}")
            ).scalar()
    return counts
