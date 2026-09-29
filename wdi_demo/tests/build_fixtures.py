"""
Builds small, realistic fixture files that mimic the exact response shapes
of the World Bank and UN Data Portal APIs, dropped straight into a cache
directory so fetch/worldbank.py and fetch/un_wpp.py exercise their real
parsing/filtering logic without any network access. Used by
tests/run_pipeline_test.py.
"""

import json
import os

FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), "fixtures_cache")

TEST_WDI_CODES = ["NY.GDP.MKTP.CD", "SP.POP.TOTL", "XX.BOGUS.CODE"]
TEST_WPP_IDS = [49, 61, 9999]
YEAR_START, YEAR_END = 2018, 2020


def _write_json(rel_path, payload):
    path = os.path.join(FIXTURE_ROOT, "worldbank", rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)


def _write_csv(rel_path, header, rows):
    path = os.path.join(FIXTURE_ROOT, "un_wpp", rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("sep=|\n")
        f.write("|".join(header) + "\n")
        for row in rows:
            f.write("|".join(str(row.get(h, "")) for h in header) + "\n")


def build():
    # --- World Bank: countries (3 real + 1 aggregate to be excluded) -----
    # NOTE: fetch_countries()'s cache stores the already-paginated FLAT list
    # of records (matching what list(_paginate(...)) produces), not the raw
    # [meta, records] envelope -- so the fixture must be a flat list too.
    _write_json("countries.json", [
        {"id": "USA", "iso2Code": "US", "name": "United States",
         "region": {"value": "North America"},
         "incomeLevel": {"value": "High income"},
         "lendingType": {"value": "Not classified"},
         "capitalCity": "Washington D.C.", "longitude": "-77.032", "latitude": "38.8895"},
        {"id": "FRA", "iso2Code": "FR", "name": "France",
         "region": {"value": "Europe & Central Asia"},
         "incomeLevel": {"value": "High income"},
         "lendingType": {"value": "Not classified"},
         "capitalCity": "Paris", "longitude": "2.3316", "latitude": "48.8566"},
        {"id": "DEU", "iso2Code": "DE", "name": "Germany",
         "region": {"value": "Europe & Central Asia"},
         "incomeLevel": {"value": "High income"},
         "lendingType": {"value": "Not classified"},
         "capitalCity": "Berlin", "longitude": "13.4115", "latitude": "52.5235"},
        {"id": "ARB", "iso2Code": "1A", "name": "Arab World",
         "region": {"value": "Aggregates"},
         "incomeLevel": {"value": "Aggregates"},
         "lendingType": {"value": "Aggregates"},
         "capitalCity": "", "longitude": "", "latitude": ""},
    ])

    # --- World Bank: indicator metadata (2 valid, 1 bogus/skip) -----------
    _write_json("indicator_meta_NY.GDP.MKTP.CD.json", [
        {"page": 1, "pages": 1},
        [{"id": "NY.GDP.MKTP.CD", "name": "GDP (current US$)",
          "sourceNote": "GDP at purchaser's prices...", "sourceOrganization": "World Bank"}],
    ])
    _write_json("indicator_meta_SP.POP.TOTL.json", [
        {"page": 1, "pages": 1},
        [{"id": "SP.POP.TOTL", "name": "Population, total",
          "sourceNote": "Total population is based on...", "sourceOrganization": "(1) UN Population Division..."}],
    ])
    _write_json("indicator_meta_XX.BOGUS.CODE.json", [
        {"message": [{"id": "120", "key": "Invalid value", "value": "The provided parameter value is not valid"}]},
    ])

    # --- World Bank: observations ------------------------------------------
    # includes: one null value (must be filtered), one aggregate-country row
    # (must be filtered), one valid row per real country/year.
    def obs_records(indicator_id, base_value):
        recs = []
        for iso3 in ("USA", "FRA", "DEU"):
            for year in (2018, 2019, 2020):
                recs.append({
                    "indicator": {"id": indicator_id, "value": "x"},
                    "country": {"id": iso3, "value": iso3},
                    "countryiso3code": iso3,
                    "date": str(year),
                    "value": base_value + (ord(iso3[0]) + year) % 7,
                    "unit": "", "obs_status": "", "decimal": 0,
                })
        # null-value row that must be dropped
        recs.append({
            "indicator": {"id": indicator_id, "value": "x"}, "country": {"id": "USA", "value": "USA"},
            "countryiso3code": "USA", "date": "2021", "value": None,
            "unit": "", "obs_status": "", "decimal": 0,
        })
        # aggregate-country row that must be dropped
        recs.append({
            "indicator": {"id": indicator_id, "value": "x"}, "country": {"id": "ARB", "value": "Arab World"},
            "countryiso3code": "ARB", "date": "2019", "value": 999999,
            "unit": "", "obs_status": "", "decimal": 0,
        })
        return recs

    # NOTE: fetch_observations()'s cache is likewise a flat list of records,
    # not the raw [meta, records] envelope.
    _write_json(f"obs_NY.GDP.MKTP.CD_{YEAR_START}_{YEAR_END}.json",
                obs_records("NY.GDP.MKTP.CD", 1_000_000))
    _write_json(f"obs_SP.POP.TOTL_{YEAR_START}_{YEAR_END}.json",
                obs_records("SP.POP.TOTL", 10_000_000))

    # --- UN: locations (3 real countries + 1 non-country + coverage of an
    # iso3 that WDI doesn't have, to test the no-matching-country skip) ----
    loc_header = ["Id", "ParentId", "Name", "Iso3", "Iso2", "LocationTypeId", "LocationType", "Longitude", "Latitude"]
    _write_csv("locations.csv", loc_header, [
        {"Id": 840, "ParentId": 1, "Name": "United States of America", "Iso3": "USA", "Iso2": "US",
         "LocationTypeId": 4, "LocationType": "Country/Area", "Longitude": -100.0, "Latitude": 40.0},
        {"Id": 250, "ParentId": 1, "Name": "France", "Iso3": "FRA", "Iso2": "FR",
         "LocationTypeId": 4, "LocationType": "Country/Area", "Longitude": 2.0, "Latitude": 46.0},
        {"Id": 276, "ParentId": 1, "Name": "Germany", "Iso3": "DEU", "Iso2": "DE",
         "LocationTypeId": 4, "LocationType": "Country/Area", "Longitude": 10.0, "Latitude": 51.0},
        {"Id": 999, "ParentId": 1, "Name": "Atlantis", "Iso3": "ATL", "Iso2": "AT",
         "LocationTypeId": 4, "LocationType": "Country/Area", "Longitude": 0.0, "Latitude": 0.0},
        {"Id": 1, "ParentId": "", "Name": "World", "Iso3": "", "Iso2": "",
         "LocationTypeId": 1, "LocationType": "World", "Longitude": "", "Latitude": ""},
    ])

    # --- UN: indicators (2 valid, 1 bogus/skip: 9999 simply absent) --------
    ind_header = ["IndicatorId", "IndicatorName", "ShortName"]
    _write_csv("indicators.csv", ind_header, [
        {"IndicatorId": 49, "IndicatorName": "Total population by sex", "ShortName": "TPopulation"},
        {"IndicatorId": 61, "IndicatorName": "Life expectancy at birth by sex", "ShortName": "E0"},
    ])

    # --- UN: observations (Median variant kept, High variant dropped by
    # filter, Atlantis row dropped for lacking a matching WDI country) -----
    data_header = [
        "LocationId", "Location", "Iso3", "Iso2", "IndicatorId", "Indicator",
        "VariantId", "Variant", "TimeId", "TimeLabel", "SexId", "Sex", "Value",
    ]

    def wpp_rows(indicator_id, base_value):
        rows = []
        for loc_id, iso3 in ((840, "USA"), (250, "FRA"), (276, "DEU")):
            for year in (2018, 2019, 2020):
                for sex_id, sex in ((3, "Both sexes"),):
                    rows.append({
                        "LocationId": loc_id, "Location": iso3, "Iso3": iso3, "Iso2": iso3[:2],
                        "IndicatorId": indicator_id, "Indicator": "x",
                        "VariantId": 4, "Variant": "Median",
                        "TimeId": year, "TimeLabel": year,
                        "SexId": sex_id, "Sex": sex,
                        "Value": base_value + (ord(iso3[0]) + year) % 5,
                    })
        # High-variant row that the variant filter must drop
        rows.append({
            "LocationId": 840, "Location": "USA", "Iso3": "USA", "Iso2": "US",
            "IndicatorId": indicator_id, "Indicator": "x",
            "VariantId": 9, "Variant": "High", "TimeId": 2019, "TimeLabel": 2019,
            "SexId": 3, "Sex": "Both sexes", "Value": 424242,
        })
        # Atlantis row: valid Median variant, but no matching WDI country
        rows.append({
            "LocationId": 999, "Location": "ATL", "Iso3": "ATL", "Iso2": "AT",
            "IndicatorId": indicator_id, "Indicator": "x",
            "VariantId": 4, "Variant": "Median", "TimeId": 2019, "TimeLabel": 2019,
            "SexId": 3, "Sex": "Both sexes", "Value": 123456,
        })
        return rows

    _write_csv(f"obs_49_{YEAR_START}_{YEAR_END}_batch1.csv", data_header, wpp_rows(49, 300))
    _write_csv(f"obs_61_{YEAR_START}_{YEAR_END}_batch1.csv", data_header, wpp_rows(61, 70))

    print(f"Fixtures written under {FIXTURE_ROOT}")


if __name__ == "__main__":
    build()
