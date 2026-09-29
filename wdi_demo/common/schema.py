"""
Shared relational schema, defined once with SQLAlchemy Core so the exact
same definition produces correct DDL on both Postgres and MySQL/MariaDB.

Shape (6 tables, 2 source systems joined through one shared dimension):

    countries (shared dimension)
        country_id PK  <--- iso3 code, e.g. "USA"
        ...
          ^                                ^
          |                                |
    wdi_observations                 wpp_observations
    (country_id, indicator_code,     (country_id, indicator_id,
     year) -> value                   variant_id, sex, year) -> value
          |                                |
    wdi_indicators                   wpp_indicators, wpp_variants

This is deliberately NOT "one CSV -> one table": WDI and MDI observations
are both long/tidy fact tables that only make sense joined back to the
shared `countries` dimension and their own indicator/variant lookup
tables, and the two fact tables can themselves be joined to each other
through `countries` for cross-source demo queries (e.g. GDP growth next to
population growth, by region and year).
"""

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

countries = Table(
    "countries",
    metadata,
    Column("country_id", String(3), primary_key=True),  # World Bank / ISO alpha-3
    Column("iso2", String(2)),
    Column("un_location_id", Integer, index=True, nullable=True),
    Column("name", String(100), nullable=False),
    Column("region", String(100)),
    Column("income_group", String(100)),
    Column("lending_type", String(100)),
    Column("capital_city", String(100)),
    Column("longitude", Float, nullable=True),
    Column("latitude", Float, nullable=True),
)

wdi_indicators = Table(
    "wdi_indicators",
    metadata,
    Column("indicator_code", String(64), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("source_note", Text),
    Column("source_organization", Text),
)

wdi_observations = Table(
    "wdi_observations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("country_id", String(3), ForeignKey("countries.country_id"), nullable=False, index=True),
    Column("indicator_code", String(64), ForeignKey("wdi_indicators.indicator_code"), nullable=False, index=True),
    Column("year", SmallInteger, nullable=False),
    Column("value", Float, nullable=True),
    UniqueConstraint("country_id", "indicator_code", "year", name="uq_wdi_obs"),
)

wpp_indicators = Table(
    "wpp_indicators",
    metadata,
    Column("indicator_id", Integer, primary_key=True),
    Column("short_name", String(64), nullable=False),
    Column("name", String(255), nullable=False),
)

wpp_variants = Table(
    "wpp_variants",
    metadata,
    Column("variant_id", Integer, primary_key=True),
    Column("name", String(64), nullable=False),
)

wpp_observations = Table(
    "wpp_observations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("country_id", String(3), ForeignKey("countries.country_id"), nullable=False, index=True),
    Column("indicator_id", Integer, ForeignKey("wpp_indicators.indicator_id"), nullable=False, index=True),
    Column("variant_id", Integer, ForeignKey("wpp_variants.variant_id"), nullable=False, index=True),
    Column("sex", String(20), nullable=True),
    Column("year", SmallInteger, nullable=False),
    Column("value", Float, nullable=True),
    UniqueConstraint("country_id", "indicator_id", "variant_id", "sex", "year", name="uq_wpp_obs"),
)

# Load order matters for FK constraints.
TABLE_LOAD_ORDER = [
    countries,
    wdi_indicators,
    wpp_indicators,
    wpp_variants,
    wdi_observations,
    wpp_observations,
]

FACT_TABLES = [wdi_observations, wpp_observations]
