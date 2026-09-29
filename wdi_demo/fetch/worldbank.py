"""
Fetch country metadata and indicator observations from the World Bank
Indicators API v2 (https://api.worldbank.org/v2/...).

This is a very stable, extensively documented public API (see
https://datahelpdesk.worldbank.org/knowledgebase/articles/898581), but this
module still validates every indicator code against the live metadata
endpoint before pulling observations, and skips (with a clear, logged
reason) anything the API doesn't recognize any more -- e.g. if the World
Bank has retired or renamed a code since this script's curated list was
written. It never silently drops data without saying so.
"""

import json
import os

import config
from fetch.http_util import get

API_BASE = "https://api.worldbank.org/v2"
CACHE_DIR = os.path.join(config.CACHE_DIR, "worldbank")


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _cached_json(cache_name, fetch_fn):
    path = _cache_path(cache_name)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    data = fetch_fn()
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def _paginate(url):
    """Yield every record across all pages of a World Bank v2 endpoint."""
    page = 1
    while True:
        resp = get(url, params={"format": "json", "per_page": 1000, "page": page})
        payload = resp.json()
        if not isinstance(payload, list) or len(payload) < 2:
            # World Bank returns a single-element error object on bad requests.
            raise RuntimeError(f"Unexpected response shape from {url}: {payload!r}")
        meta, records = payload[0], payload[1]
        if records is None:
            break
        for record in records:
            yield record
        pages = meta.get("pages", 1) or 1
        if page >= pages:
            break
        page += 1


def fetch_countries():
    """Return (country_rows, valid_country_ids) for real countries only,
    excluding the World Bank's own regional/income-group aggregates (which
    are modeled as fake "countries" with region.value == 'Aggregates')."""

    def _fetch():
        return list(_paginate(f"{API_BASE}/country"))

    raw = _cached_json("countries.json", _fetch)

    rows = []
    for c in raw:
        region = (c.get("region") or {}).get("value")
        if region in (None, "Aggregates"):
            continue
        rows.append({
            "country_id": c["id"],
            "iso2": c.get("iso2Code"),
            "name": c.get("name"),
            "region": region,
            "income_group": (c.get("incomeLevel") or {}).get("value"),
            "lending_type": (c.get("lendingType") or {}).get("value"),
            "capital_city": c.get("capitalCity") or None,
            "longitude": float(c["longitude"]) if c.get("longitude") else None,
            "latitude": float(c["latitude"]) if c.get("latitude") else None,
        })
    valid_ids = {r["country_id"] for r in rows}
    return rows, valid_ids


def validate_indicators(codes):
    """Check each candidate indicator code against the metadata endpoint.
    Returns (valid_rows, skipped) where valid_rows is a list of dicts ready
    for the wdi_indicators table, and skipped is {code: reason}."""
    valid_rows = []
    skipped = {}

    for code in codes:
        cache_name = f"indicator_meta_{code}.json"
        path = _cache_path(cache_name)
        if os.path.exists(path):
            with open(path, "r") as f:
                payload = json.load(f)
        else:
            try:
                resp = get(f"{API_BASE}/indicator/{code}", params={"format": "json"})
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                skipped[code] = f"request failed: {exc}"
                continue
            with open(path, "w") as f:
                json.dump(payload, f)

        if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
            skipped[code] = "indicator code not recognized by the API"
            continue

        meta = payload[1][0]
        valid_rows.append({
            "indicator_code": code,
            "name": meta.get("name") or code,
            "source_note": meta.get("sourceNote"),
            "source_organization": meta.get("sourceOrganization"),
        })

    return valid_rows, skipped


def fetch_observations(code, valid_country_ids, year_start, year_end):
    """Return wdi_observations rows for one indicator, restricted to real
    countries and non-null values."""

    def _fetch():
        url = f"{API_BASE}/country/all/indicator/{code}"
        records = []
        for rec in _paginate_with_date(url, year_start, year_end):
            records.append(rec)
        return records

    cache_name = f"obs_{code}_{year_start}_{year_end}.json"
    raw = _cached_json(cache_name, _fetch)

    rows = []
    for rec in raw:
        iso3 = rec.get("countryiso3code")
        if not iso3 or iso3 not in valid_country_ids:
            continue
        if rec.get("value") is None:
            continue
        try:
            year = int(rec["date"])
        except (TypeError, ValueError):
            continue
        rows.append({
            "country_id": iso3,
            "indicator_code": code,
            "year": year,
            "value": float(rec["value"]),
        })
    return rows


def _paginate_with_date(url, year_start, year_end):
    page = 1
    while True:
        resp = get(url, params={
            "format": "json",
            "per_page": 20000,
            "page": page,
            "date": f"{year_start}:{year_end}",
        })
        payload = resp.json()
        if not isinstance(payload, list) or len(payload) < 2:
            raise RuntimeError(f"Unexpected response shape from {url}: {payload!r}")
        meta, records = payload[0], payload[1]
        if records is None:
            break
        for record in records:
            yield record
        pages = meta.get("pages", 1) or 1
        if page >= pages:
            break
        page += 1
