# WDI + WPP2024 demo-data loader

Loads a curated, genuinely relational subset of two real global datasets into
your own Postgres or MySQL database (including Aiven-hosted instances of
either):

- **World Bank World Development Indicators (WDI)** -- ~75 well-known
  economic/demographic/health/education indicators, ~217 countries, annual,
  1990-2024.
- **UN World Population Prospects 2024 (WPP2024)** -- 8 core demographic
  indicators (population by sex, fertility, life expectancy, migration, birth
  and death rates, median age), same country/year coverage.

Both sources are joined through one shared `countries` table, so the result
is six related tables, not a single flat CSV-to-table dump:

```
countries  <---  wdi_observations  --->  wdi_indicators
    ^
    |------------ wpp_observations ---> wpp_indicators
                        |
                        ---------------> wpp_variants
```

## Before you run it: does it actually fit?

With the default settings this comes out to roughly **50-100 MB** total
(data + indexes) -- comfortably inside a 500 MB budget. The script doesn't
just assume that, though:

1. `python build_db.py --dry-run` fetches everything (using a local
   on-disk cache, so it's cheap to re-run), prints the real row counts, and
   estimates the on-disk size *before* touching any database.
2. If that estimate exceeds `MAX_DB_BYTES` (default 400 MB, see `.env.example`),
   it refuses to load and tells you what to change (fewer years, fewer
   indicators, or a higher budget if you actually have the room).
3. After a real load, it queries the database's own catalog
   (`pg_total_relation_size` / `information_schema.tables`) for the
   *measured* size of every table -- not another estimate.

You have real headroom here: if you'd rather have more history or more
indicators, see "Widening the scope" below.

## Setup

```bash
cd wdi_demo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

- Easiest: paste Aiven's own "Service URI" into `DATABASE_URL` (works for
  either a Postgres or a MySQL service -- the script rewrites the scheme to
  the right driver automatically).
- Or fill in the discrete `DB_ENGINE` / `DB_HOST` / `DB_PORT` / `DB_USER` /
  `DB_PASSWORD` / `DB_NAME` fields instead, if that's what you copied from
  the console.

Aiven requires TLS for both engines. `DB_SSLMODE=require` (the default) is
encrypted but doesn't verify the server certificate, which matches what
Aiven's own quickstarts use. For strict verification, download the service's
CA certificate from the Aiven console and set `DB_SSL_CA_PATH` to its path.

## Run it

```bash
python build_db.py --dry-run   # see row counts + size estimate, touch nothing
python build_db.py             # create the schema and load it for real
```

It's safe to re-run: existing rows are left alone (`ON CONFLICT DO NOTHING` /
`INSERT ... ON DUPLICATE KEY UPDATE`), so a re-run after a network hiccup
just fills in whatever didn't make it in last time. Pass `--drop-first` if
you want a clean slate instead.

A full run takes a few minutes -- it's making a few dozen paginated calls to
two public APIs, not one big bulk download. Each response is cached under
`.cache/`, so if you stop and restart, only the missing pieces get re-fetched.

## Example queries once it's loaded

GDP per capita alongside population growth, most recent year, joined through
the shared `countries` table:

```sql
SELECT c.name, c.region,
       gdp.value AS gdp_per_capita_usd,
       pop.value AS population_growth_pct
FROM countries c
JOIN wdi_observations gdp ON gdp.country_id = c.country_id
                          AND gdp.indicator_code = 'NY.GDP.PCAP.CD'
                          AND gdp.year = 2023
JOIN wdi_observations pop ON pop.country_id = c.country_id
                          AND pop.indicator_code = 'SP.POP.GROW'
                          AND pop.year = 2023
ORDER BY gdp.value DESC
LIMIT 20;
```

Life expectancy trend (UN WPP) next to health spending (WDI), by country,
over time:

```sql
SELECT c.name, wpp.year,
       wpp.value AS life_expectancy_years,
       wdi.value AS health_spend_pct_gdp
FROM countries c
JOIN wpp_observations wpp ON wpp.country_id = c.country_id
JOIN wpp_indicators wi ON wi.indicator_id = wpp.indicator_id AND wi.short_name = 'E0'
JOIN wdi_observations wdi ON wdi.country_id = c.country_id
                          AND wdi.indicator_code = 'SH.XPD.CHEX.GD.ZS'
                          AND wdi.year = wpp.year
WHERE wpp.sex = 'Both sexes' AND c.country_id = 'BRA'
ORDER BY wpp.year;
```

## Widening the scope

Everything that controls size lives in `config.py` (or override via `.env`):

- `WDI_YEAR_START` / `WPP_YEAR_START` -- push back to 1960 for much deeper
  history (WDI) -- still tiny at this indicator count.
- `WDI_INDICATOR_CODES` -- add more codes from
  https://databank.worldbank.org/ ; unrecognized codes are skipped
  automatically with a logged reason, not a crash.
- `WPP_INCLUDE_PROJECTIONS=1` -- adds the UN's Medium-variant projections
  out to 2100, not just historical data.
- `MAX_DB_BYTES` -- raise this once you've confirmed how much room you
  actually have.

## How this was verified

The DB schema, bulk-insert logic (including the conflict-safe re-run
behavior), and size-measurement queries were tested end-to-end against real
local Postgres and MariaDB instances. The API parsing/filtering logic
(aggregate-country exclusion, indicator validation/skip, variant filtering,
null-value handling) was tested against fixture data built to match the
exact response shapes documented by the World Bank Indicators API and the UN
Population Division Data Portal API. Live network access to those two APIs
wasn't available from the environment this was built in, so run
`--dry-run` first thing on your own machine to confirm the live shapes still
match before doing a full load -- the script validates every indicator code
and location type at runtime and will tell you clearly if either API has
changed since this was written, rather than silently loading bad data.

## Files

```
config.py            All tunable scope/size settings.
build_db.py           CLI entry point (--dry-run / --drop-first / --force).
common/schema.py       The 6-table SQLAlchemy schema, shared by both engines.
common/db.py           Engine/connection setup, schema creation, bulk insert,
                       size measurement.
fetch/worldbank.py     World Bank API client (countries, indicator metadata,
                       observations).
fetch/un_wpp.py        UN Data Portal API client (locations, indicators,
                       observations).
tests/                 Fixture-based tests exercising the full pipeline
                       against real local Postgres/MySQL, without needing
                       network access.
```
