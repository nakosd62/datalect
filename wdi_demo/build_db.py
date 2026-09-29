#!/usr/bin/env python3
"""
Load a curated, relational subset of the World Bank's World Development
Indicators (WDI) and the UN's World Population Prospects 2024 (WPP2024)
into a Postgres or MySQL database -- including Aiven-hosted instances of
either.

Usage:
    python build_db.py --dry-run     # estimate size, fetch+cache data, touch nothing
    python build_db.py               # actually create the schema and load it
    python build_db.py --drop-first  # drop existing tables first (careful!)
    python build_db.py --force       # load even if the size estimate exceeds MAX_DB_BYTES

Configuration lives in .env (see .env.example) and config.py.
"""

import argparse
import sys
import time

# Must run before `import config` (and before fetch/common import config's
# values at module scope) so that anything set in .env -- MAX_DB_BYTES,
# WDI_YEAR_START, CACHE_DIR, etc -- actually takes effect, instead of
# config.py silently falling back to its hardcoded defaults.
from dotenv import load_dotenv
load_dotenv()

import config
from common import db
from common.schema import (
    countries as countries_t,
    wdi_indicators as wdi_indicators_t,
    wdi_observations as wdi_observations_t,
    wpp_indicators as wpp_indicators_t,
    wpp_observations as wpp_observations_t,
    wpp_variants as wpp_variants_t,
)
from fetch import un_wpp, worldbank


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def gather_data(args):
    """Fetch (and cache) everything from both source APIs. Returns a dict
    of table_name -> list-of-row-dicts, ready to insert in
    common.schema.TABLE_LOAD_ORDER order."""

    print("== World Bank: country metadata ==")
    wdi_countries, valid_wb_ids = worldbank.fetch_countries()
    print(f"  {len(wdi_countries)} real countries/economies (aggregates excluded)")

    print("== World Bank: validating curated indicator list ==")
    wdi_indicator_rows, wdi_skipped = worldbank.validate_indicators(config.WDI_INDICATOR_CODES)
    print(f"  {len(wdi_indicator_rows)} valid / {len(wdi_skipped)} skipped")
    for code, reason in wdi_skipped.items():
        print(f"    SKIP {code}: {reason}")

    print(f"== World Bank: fetching observations ({config.WDI_YEAR_START}-{config.WDI_YEAR_END}) ==")
    wdi_observation_rows = []
    for i, row in enumerate(wdi_indicator_rows, start=1):
        code = row["indicator_code"]
        obs = worldbank.fetch_observations(code, valid_wb_ids, config.WDI_YEAR_START, config.WDI_YEAR_END)
        wdi_observation_rows.extend(obs)
        print(f"  [{i}/{len(wdi_indicator_rows)}] {code}: {len(obs)} rows (running total {len(wdi_observation_rows)})")

    print("== UN Data Portal: location metadata ==")
    wpp_locations = un_wpp.fetch_locations()
    print(f"  {len(wpp_locations)} country/area locations")

    print("== UN Data Portal: validating curated indicator list ==")
    wpp_valid, wpp_skipped = un_wpp.validate_indicators(config.WPP_INDICATORS.keys())
    for iid, reason in wpp_skipped.items():
        print(f"    SKIP {iid} ({config.WPP_INDICATORS[iid]}): {reason}")
    wpp_indicator_rows = [
        {"indicator_id": iid, "short_name": config.WPP_INDICATORS[iid], "name": name}
        for iid, name in wpp_valid.items()
    ]

    # Build the shared countries table: anchored on the WDI list, enriched
    # with the UN's numeric location id wherever the ISO3 codes line up.
    wpp_iso3_to_locid = {loc["iso3"]: loc["un_location_id"] for loc in wpp_locations if loc["iso3"]}
    for row in wdi_countries:
        row["un_location_id"] = wpp_iso3_to_locid.get(row["country_id"])
    unmatched = sum(1 for r in wdi_countries if r["un_location_id"] is None)
    if unmatched:
        print(f"  note: {unmatched} WDI countries have no matching UN location id (kept, just un-joinable to WPP)")

    valid_country_iso3 = {r["country_id"] for r in wdi_countries}
    un_location_ids = [loc["un_location_id"] for loc in wpp_locations]

    year_end = config.WPP_YEAR_END if not config.WPP_INCLUDE_PROJECTIONS else max(config.WPP_YEAR_END, 2100)
    print(f"== UN Data Portal: fetching observations ({config.WPP_YEAR_START}-{year_end}, variant='{config.WPP_VARIANT_FILTER}') ==")

    wpp_observation_rows = []
    wpp_variant_rows = {}
    skipped_no_country = 0
    for i, iid in enumerate(wpp_valid.keys(), start=1):
        raw_rows = un_wpp.fetch_observations(
            iid, un_location_ids, config.WPP_YEAR_START, year_end,
            variant_filter=config.WPP_VARIANT_FILTER,
        )
        kept = 0
        for r in raw_rows:
            iso3 = (r.get("Iso3") or "").strip()
            if iso3 not in valid_country_iso3:
                skipped_no_country += 1
                continue
            if r.get("Value") in (None, ""):
                continue
            try:
                variant_id = int(r["VariantId"])
                year = int(float(r["TimeLabel"])) if r.get("TimeLabel") else int(float(r["TimeId"]))
                value = float(r["Value"])
            except (KeyError, ValueError, TypeError):
                continue
            wpp_variant_rows[variant_id] = r.get("Variant") or str(variant_id)
            wpp_observation_rows.append({
                "country_id": iso3,
                "indicator_id": iid,
                "variant_id": variant_id,
                "sex": r.get("Sex"),
                "year": year,
                "value": value,
            })
            kept += 1
        print(f"  [{i}/{len(wpp_valid)}] indicator {iid} ({config.WPP_INDICATORS[iid]}): {kept} rows kept")

    if skipped_no_country:
        print(f"  note: {skipped_no_country} UN observation rows skipped (no matching WDI country by ISO3)")

    wpp_variant_row_list = [{"variant_id": vid, "name": name} for vid, name in wpp_variant_rows.items()]

    return {
        "countries": wdi_countries,
        "wdi_indicators": wdi_indicator_rows,
        "wpp_indicators": wpp_indicator_rows,
        "wpp_variants": wpp_variant_row_list,
        "wdi_observations": wdi_observation_rows,
        "wpp_observations": wpp_observation_rows,
    }


def estimate_bytes(data):
    return (
        len(data["wdi_observations"]) * config.BYTES_PER_WDI_ROW
        + len(data["wpp_observations"]) * config.BYTES_PER_WPP_ROW
    )


def print_size_report(data, label):
    wdi_n = len(data["wdi_observations"])
    wpp_n = len(data["wpp_observations"])
    estimate = estimate_bytes(data)
    print(f"\n== {label}: projected size ==")
    print(f"  wdi_observations: {wdi_n:,} rows")
    print(f"  wpp_observations: {wpp_n:,} rows")
    print(f"  estimated on-disk size (data + indexes): {human_bytes(estimate)}")
    print(f"  configured budget (MAX_DB_BYTES):         {human_bytes(config.MAX_DB_BYTES)}")
    return estimate


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Fetch/estimate only; never touch the database.")
    parser.add_argument("--drop-first", action="store_true", help="Drop existing tables before creating them.")
    parser.add_argument("--force", action="store_true", help="Load even if the size estimate exceeds MAX_DB_BYTES.")
    args = parser.parse_args()

    start = time.time()
    data = gather_data(args)
    estimate = print_size_report(data, "Dry run" if args.dry_run else "Pre-load estimate")

    if estimate > config.MAX_DB_BYTES and not args.force:
        print(
            "\nRefusing to proceed: the projected size exceeds MAX_DB_BYTES.\n"
            "Options: shrink WDI_YEAR_START/WPP_YEAR_START, trim "
            "config.WDI_INDICATOR_CODES, raise MAX_DB_BYTES if you actually "
            "have more room, or pass --force to load anyway."
        )
        sys.exit(1)

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to actually load the database.")
        return

    print("\n== Connecting to the database ==")
    engine = db.build_engine()
    db.wait_for_connection(engine)
    print(f"  connected ({engine.dialect.name})")

    print("== Creating schema ==")
    db.create_schema(engine, drop_first=args.drop_first)

    load_plan = [
        (countries_t, data["countries"]),
        (wdi_indicators_t, data["wdi_indicators"]),
        (wpp_indicators_t, data["wpp_indicators"]),
        (wpp_variants_t, data["wpp_variants"]),
        (wdi_observations_t, data["wdi_observations"]),
        (wpp_observations_t, data["wpp_observations"]),
    ]

    print("== Loading tables ==")
    for table, rows in load_plan:
        inserted = db.bulk_insert(engine, table, rows, config.INSERT_CHUNK_SIZE)
        print(f"  {table.name}: {inserted:,} rows submitted")

    print("\n== Measured, on-disk sizes (ground truth, not an estimate) ==")
    sizes = db.measured_table_sizes(engine)
    counts = db.row_counts(engine)
    total = 0
    for name, size in sizes.items():
        total += size
        print(f"  {name:<20} {counts.get(name, '?'):>10,} rows   {human_bytes(size):>10}")
    print(f"  {'TOTAL':<20} {'':>10}   {human_bytes(total):>10}")

    if total > config.MAX_DB_BYTES:
        print(
            f"\nWARNING: measured size ({human_bytes(total)}) exceeds your configured "
            f"budget ({human_bytes(config.MAX_DB_BYTES)}). Check your actual free "
            "space on the Aiven service before loading more."
        )

    print(f"\nDone in {time.time() - start:.0f}s.")


if __name__ == "__main__":
    main()
