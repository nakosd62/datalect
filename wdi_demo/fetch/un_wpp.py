"""
Fetch location metadata and indicator observations from the UN Population
Division's Data Portal API (https://population.un.org/dataportalapi/),
which serves World Population Prospects 2024 (WPP2024) data.

Verified response shapes (confirmed against the API's own docs/examples,
not guessed):
  - GET /api/v1/indicators?format=csv          -> IndicatorId, IndicatorName, ...
  - GET /api/v1/locations?format=csv           -> Id, ParentId, Name, Iso3, Iso2,
                                                   LocationTypeId, LocationType, ...
  - GET /api/v1/data/indicators/{id}/locations/{ids}/start/{y1}/end/{y2}/?format=csv
        -> LocationId, Location, Iso3, Iso2, IndicatorId, Indicator,
           VariantId, Variant, TimeId, TimeLabel, SexId, Sex, Value, ...

All responses are pipe (|) delimited and begin with a literal `sep=|` line
that must be skipped before parsing.
"""

import csv
import io
import os

import config
from fetch.http_util import get

API_BASE = "https://population.un.org/dataportalapi/api/v1"
CACHE_DIR = os.path.join(config.CACHE_DIR, "un_wpp")

COUNTRY_AREA_TYPE = "country/area"  # matched case-insensitively


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _parse_pipe_csv(text: str):
    lines = text.splitlines()
    if lines and lines[0].strip().lower().startswith("sep="):
        lines = lines[1:]
    reader = csv.DictReader(lines, delimiter="|")
    return list(reader)


def _cached_csv(cache_name, url, params):
    path = _cache_path(cache_name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return _parse_pipe_csv(f.read())
    resp = get(url, params=params)
    with open(path, "w", encoding="utf-8") as f:
        f.write(resp.text)
    return _parse_pipe_csv(resp.text)


def fetch_locations():
    """Return all locations the API knows about, each as a dict with
    un_location_id/iso3/iso2/name/location_type, plus the list of distinct
    location types actually seen (for diagnostics if the expected
    'Country/Area' type isn't found -- rather than failing silently)."""
    rows = _cached_csv(
        "locations.csv",
        f"{API_BASE}/locations",
        {"sort": "id", "format": "csv"},
    )

    seen_types = sorted({r.get("LocationType", "") for r in rows})
    countries = []
    for r in rows:
        if (r.get("LocationType") or "").strip().lower() != COUNTRY_AREA_TYPE:
            continue
        try:
            loc_id = int(r["Id"])
        except (KeyError, ValueError):
            continue
        countries.append({
            "un_location_id": loc_id,
            "iso3": (r.get("Iso3") or "").strip() or None,
            "iso2": (r.get("Iso2") or "").strip() or None,
            "name": r.get("Name"),
            "location_type": r.get("LocationType"),
        })

    if not countries:
        raise RuntimeError(
            "No locations matched LocationType == 'Country/Area'. The API's "
            f"distinct location types were: {seen_types!r}. Update "
            "fetch/un_wpp.py's COUNTRY_AREA_TYPE to match."
        )
    return countries


def validate_indicators(indicator_ids):
    """Check configured indicator IDs against the live /indicators list.
    Returns (valid: {id: name}, skipped: {id: reason})."""
    rows = _cached_csv(
        "indicators.csv",
        f"{API_BASE}/indicators",
        {"sort": "name", "format": "csv"},
    )
    by_id = {}
    for r in rows:
        try:
            by_id[int(r["IndicatorId"])] = r.get("IndicatorName") or r.get("ShortName") or str(r["IndicatorId"])
        except (KeyError, ValueError):
            continue

    valid, skipped = {}, {}
    for iid in indicator_ids:
        if iid in by_id:
            valid[iid] = by_id[iid]
        else:
            skipped[iid] = "indicator id not present in the live /indicators list"
    return valid, skipped


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_observations(indicator_id, un_location_ids, year_start, year_end, variant_filter=None, location_batch_size=50):
    """Yield raw parsed CSV rows (dicts) for one indicator across all given
    UN location ids, batching the location list to keep request URLs short.
    Filters to `variant_filter` (substring match on the 'Variant' column,
    case-insensitive) when given.
    """
    all_rows = []
    batches = list(_chunk(sorted(un_location_ids), location_batch_size))
    for batch_num, batch in enumerate(batches, start=1):
        locations_str = ",".join(str(x) for x in batch)
        cache_name = f"obs_{indicator_id}_{year_start}_{year_end}_batch{batch_num}.csv"
        url = (
            f"{API_BASE}/data/indicators/{indicator_id}/locations/{locations_str}"
            f"/start/{year_start}/end/{year_end}/"
        )
        rows = _cached_csv(cache_name, url, {"format": "csv"})
        all_rows.extend(rows)

    if variant_filter:
        vf = variant_filter.strip().lower()
        all_rows = [r for r in all_rows if vf in (r.get("Variant") or "").strip().lower()]

    return all_rows
