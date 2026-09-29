"""
End-to-end test of the full pipeline (fetch parsing -> transform -> DB load)
against the fixtures in tests/fixtures_cache/, run against a REAL local
Postgres and/or MySQL instance so schema creation, bulk insert, conflict
handling, and size measurement are all exercised for real -- not mocked.

Usage:
    DATABASE_URL=postgresql+psycopg2://... python tests/run_pipeline_test.py
    DATABASE_URL=mysql+pymysql://...       python tests/run_pipeline_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from tests.build_fixtures import build as build_fixtures, FIXTURE_ROOT, TEST_WDI_CODES, TEST_WPP_IDS, YEAR_START, YEAR_END

# Point the fetch modules at the pre-built fixture cache, and narrow the
# curated lists/scope down to exactly what the fixtures cover, BEFORE
# importing build_db (which reads these config values at call time, not
# import time, so this is safe).
config.CACHE_DIR = FIXTURE_ROOT
config.WDI_INDICATOR_CODES = TEST_WDI_CODES
config.WPP_INDICATORS = {49: "TPopulation", 61: "E0", 9999: "Bogus"}
config.WDI_YEAR_START, config.WDI_YEAR_END = YEAR_START, YEAR_END
config.WPP_YEAR_START, config.WPP_YEAR_END = YEAR_START, YEAR_END
config.WPP_INCLUDE_PROJECTIONS = False
config.WPP_VARIANT_FILTER = "Median"

import build_db  # noqa: E402  (import after config overrides, see above)
from common import db  # noqa: E402
from common.schema import TABLE_LOAD_ORDER  # noqa: E402


def check(label, condition):
    status = "OK  " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(1)


def main():
    build_fixtures()

    print("\n--- gather_data() against fixtures (no network) ---")
    data = build_db.gather_data(argparse_namespace())

    countries_by_id = {c["country_id"]: c for c in data["countries"]}
    check("aggregate country (ARB) excluded", "ARB" not in countries_by_id)
    check("3 real countries present", set(countries_by_id) == {"USA", "FRA", "DEU"})
    check("un_location_id joined onto countries", countries_by_id["USA"]["un_location_id"] == 840)

    wdi_codes = {r["indicator_code"] for r in data["wdi_indicators"]}
    check("bogus WDI code skipped", "XX.BOGUS.CODE" not in wdi_codes)
    check("2 valid WDI indicators kept", wdi_codes == {"NY.GDP.MKTP.CD", "SP.POP.TOTL"})

    wdi_obs_countries = {r["country_id"] for r in data["wdi_observations"]}
    check("WDI observations exclude aggregate country", "ARB" not in wdi_obs_countries)
    # 2 indicators * 3 countries * 3 years = 18 (null + aggregate rows dropped)
    check(f"WDI observation count == 18 (got {len(data['wdi_observations'])})", len(data["wdi_observations"]) == 18)

    wpp_ind_ids = {r["indicator_id"] for r in data["wpp_indicators"]}
    check("bogus WPP indicator id (9999) skipped", 9999 not in wpp_ind_ids)
    check("2 valid WPP indicators kept", wpp_ind_ids == {49, 61})

    wpp_obs_countries = {r["country_id"] for r in data["wpp_observations"]}
    check("WPP observations exclude Atlantis (no matching WDI country)", "ATL" not in wpp_obs_countries)
    wpp_variant_ids = {r["variant_id"] for r in data["wpp_observations"]}
    check("only the Median variant survived the filter", wpp_variant_ids == {4})
    # 2 indicators * 3 countries * 3 years = 18 (High-variant + Atlantis rows dropped)
    check(f"WPP observation count == 18 (got {len(data['wpp_observations'])})", len(data["wpp_observations"]) == 18)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("\nDATABASE_URL not set -- skipping the live-database portion of the test.")
        return

    print(f"\n--- loading into a real database: {database_url.split('@')[-1]} ---")
    os.environ.setdefault("DB_SSLMODE", "disable")
    engine = db.build_engine()
    db.wait_for_connection(engine)
    db.create_schema(engine, drop_first=True)

    from common.schema import (
        countries as countries_t, wdi_indicators as wdi_indicators_t,
        wdi_observations as wdi_observations_t, wpp_indicators as wpp_indicators_t,
        wpp_observations as wpp_observations_t, wpp_variants as wpp_variants_t,
    )
    load_plan = [
        (countries_t, data["countries"]),
        (wdi_indicators_t, data["wdi_indicators"]),
        (wpp_indicators_t, data["wpp_indicators"]),
        (wpp_variants_t, data["wpp_variants"]),
        (wdi_observations_t, data["wdi_observations"]),
        (wpp_observations_t, data["wpp_observations"]),
    ]
    for table, rows in load_plan:
        n = db.bulk_insert(engine, table, rows, config.INSERT_CHUNK_SIZE)
        print(f"  loaded {table.name}: {n} rows submitted")

    counts = db.row_counts(engine)
    check(f"countries row count == 3 (got {counts['countries']})", counts["countries"] == 3)
    check(f"wdi_observations row count == 18 (got {counts['wdi_observations']})", counts["wdi_observations"] == 18)
    check(f"wpp_observations row count == 18 (got {counts['wpp_observations']})", counts["wpp_observations"] == 18)

    # Idempotency: re-running the same insert must not duplicate or error.
    print("\n--- re-running inserts to check idempotency (ON CONFLICT DO NOTHING) ---")
    for table, rows in load_plan:
        db.bulk_insert(engine, table, rows, config.INSERT_CHUNK_SIZE)
    counts2 = db.row_counts(engine)
    check("row counts unchanged after re-insert", counts2 == counts)

    sizes = db.measured_table_sizes(engine)
    print("\nMeasured sizes:")
    for name in [t.name for t in TABLE_LOAD_ORDER]:
        print(f"  {name}: {sizes.get(name)} bytes")

    print("\nAll checks passed.")


class argparse_namespace:  # tiny stand-in, gather_data() doesn't use args yet
    dry_run = False


if __name__ == "__main__":
    main()
