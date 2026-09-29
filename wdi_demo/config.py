"""
Configuration for the WDI + WPP2024 demo-data loader.

Everything that controls *scope* (which years, which indicators, how big the
result is allowed to get) lives here so you can tune it without touching the
fetch/load logic. Sensible defaults below were chosen to comfortably fit in
roughly 500 MB of free space on a small managed database (e.g. an Aiven
free-tier plan), while still giving you a genuinely relational, multi-table,
decades-deep demo dataset.
"""

import os

# ---------------------------------------------------------------------------
# Size budget
# ---------------------------------------------------------------------------
# The loader estimates row counts and refuses to proceed (in --dry-run) or
# warns loudly (during a real run) if the projected on-disk size exceeds this
# many bytes. Override with the MAX_DB_BYTES environment variable.
# Default: 400 MB, leaving headroom under a 500 MB free-space budget.
MAX_DB_BYTES = int(os.environ.get("MAX_DB_BYTES", 400 * 1024 * 1024))

# Conservative per-row byte estimates (data + btree index overhead), used
# only for the pre-flight estimate in --dry-run. The post-load report uses
# real, measured on-disk sizes instead, so this only needs to be roughly
# right, not exact.
BYTES_PER_WDI_ROW = 110
BYTES_PER_WPP_ROW = 130

# ---------------------------------------------------------------------------
# World Bank World Development Indicators (WDI)
# ---------------------------------------------------------------------------
WDI_YEAR_START = int(os.environ.get("WDI_YEAR_START", 1990))
WDI_YEAR_END = int(os.environ.get("WDI_YEAR_END", 2024))

# A curated, cross-topic seed list of well-known, stable WDI indicator codes.
# You do NOT need to trust this list blindly: fetch/worldbank.py validates
# every single code against the live indicator-metadata endpoint before
# pulling any observations, and cleanly skips (with a logged reason) any
# code the API doesn't recognize any more, rather than crashing the load.
# Feel free to add/remove codes -- browse https://databank.worldbank.org/
# or https://api.worldbank.org/v2/indicator?format=json&per_page=25000 for more.
WDI_INDICATOR_CODES = [
    # --- Economy & growth ---------------------------------------------
    "NY.GDP.MKTP.CD",     # GDP (current US$)
    "NY.GDP.MKTP.KD",     # GDP (constant 2015 US$)
    "NY.GDP.MKTP.KD.ZG",  # GDP growth (annual %)
    "NY.GDP.PCAP.CD",     # GDP per capita (current US$)
    "NY.GDP.PCAP.KD.ZG",  # GDP per capita growth (annual %)
    "NY.GNP.PCAP.CD",     # GNI per capita, Atlas method (current US$)
    "NY.GDP.DEFL.KD.ZG",  # Inflation, GDP deflator (annual %)
    "FP.CPI.TOTL.ZG",     # Inflation, consumer prices (annual %)
    "NE.CON.TOTL.ZS",     # Final consumption expenditure (% of GDP)
    "NE.GDI.TOTL.ZS",     # Gross capital formation (% of GDP)
    "NV.AGR.TOTL.ZS",     # Agriculture, value added (% of GDP)
    "NV.IND.TOTL.ZS",     # Industry, value added (% of GDP)
    "NV.SRV.TOTL.ZS",     # Services, value added (% of GDP)
    "GC.DOD.TOTL.GD.ZS",  # Central government debt, total (% of GDP)
    "FR.INR.RINR",        # Real interest rate (%)
    "FR.INR.LEND",        # Lending interest rate (%)
    "FR.INR.DPST",        # Deposit interest rate (%)

    # --- Trade & external finance --------------------------------------
    "NE.EXP.GNFS.ZS",       # Exports of goods and services (% of GDP)
    "NE.IMP.GNFS.ZS",       # Imports of goods and services (% of GDP)
    "BX.KLT.DINV.WD.GD.ZS", # Foreign direct investment, net inflows (% of GDP)
    "BN.CAB.XOKA.GD.ZS",    # Current account balance (% of GDP)
    "DT.DOD.DECT.CD",       # External debt stocks, total (current US$)
    "BX.TRF.PWKR.CD.DT",    # Personal remittances, received (current US$)
    "PA.NUS.FCRF",          # Official exchange rate (LCU per US$)
    "FI.RES.TOTL.CD",       # Total reserves incl. gold (current US$)

    # --- Labor & poverty -------------------------------------------------
    "SL.UEM.TOTL.ZS",   # Unemployment, total (% of labor force)
    "SL.TLF.CACT.ZS",   # Labor force participation rate (% of pop 15+)
    "SL.TLF.CACT.FE.ZS",# Labor force participation rate, female (%)
    "SI.POV.GINI",      # Gini index
    "SI.POV.DDAY",      # Poverty headcount ratio at intl. poverty line (%)
    "SI.POV.NAHC",      # Poverty headcount ratio at national poverty lines (%)

    # --- Population & demographics (WDI's own view; complements WPP) ---
    "SP.POP.TOTL",        # Population, total
    "SP.POP.GROW",        # Population growth (annual %)
    "SP.URB.TOTL.IN.ZS",  # Urban population (% of total)
    "SP.RUR.TOTL.ZS",     # Rural population (% of total)
    "SP.DYN.LE00.IN",     # Life expectancy at birth, total (years)
    "SP.DYN.TFRT.IN",     # Fertility rate, total (births per woman)
    "SP.DYN.CBRT.IN",     # Birth rate, crude (per 1,000 people)
    "SP.DYN.CDRT.IN",     # Death rate, crude (per 1,000 people)
    "SH.DYN.MORT",        # Mortality rate, under-5 (per 1,000 live births)
    "SH.DYN.NMRT",        # Mortality rate, neonatal (per 1,000 live births)
    "SP.DYN.IMRT.IN",     # Mortality rate, infant (per 1,000 live births)
    "SP.ADO.TFRT",        # Adolescent fertility rate
    "SP.POP.0014.TO.ZS",  # Population ages 0-14 (% of total)
    "SP.POP.1564.TO.ZS",  # Population ages 15-64 (% of total)
    "SP.POP.65UP.TO.ZS",  # Population ages 65+ (% of total)
    "SM.POP.NETM",        # Net migration

    # --- Health -----------------------------------------------------------
    "SH.XPD.CHEX.GD.ZS",  # Current health expenditure (% of GDP)
    "SH.XPD.CHEX.PC.CD",  # Current health expenditure per capita (current US$)
    "SH.MED.BEDS.ZS",     # Hospital beds (per 1,000 people)
    "SH.MED.PHYS.ZS",     # Physicians (per 1,000 people)
    "SH.IMM.MEAS",        # Immunization, measles (% ages 12-23 months)
    "SH.IMM.IDPT",        # Immunization, DPT (% ages 12-23 months)
    "SH.STA.MMRT",        # Maternal mortality ratio (per 100,000 live births)
    "SH.STA.OWAD.ZS",     # Prevalence of overweight (% of adults)
    "SH.STA.DIAB.ZS",     # Diabetes prevalence (% of pop 20-79)

    # --- Education ----------------------------------------------------------
    "SE.ADT.LITR.ZS",     # Literacy rate, adult total (% ages 15+)
    "SE.PRM.ENRR",        # School enrollment, primary (% gross)
    "SE.SEC.ENRR",        # School enrollment, secondary (% gross)
    "SE.TER.ENRR",        # School enrollment, tertiary (% gross)
    "SE.XPD.TOTL.GD.ZS",  # Government expenditure on education (% of GDP)
    "SE.PRM.CMPT.ZS",     # Primary completion rate, total (%)

    # --- Infrastructure, energy & environment -------------------------------
    "EG.ELC.ACCS.ZS",     # Access to electricity (% of population)
    "EG.FEC.RNEW.ZS",     # Renewable energy consumption (% of total)
    "EN.ATM.CO2E.PC",     # CO2 emissions (metric tons per capita)
    "EN.ATM.CO2E.KT",     # CO2 emissions (kt)
    "IT.NET.USER.ZS",     # Individuals using the Internet (% of population)
    "IT.CEL.SETS.P2",     # Mobile cellular subscriptions (per 100 people)
    "AG.LND.AGRI.ZS",     # Agricultural land (% of land area)
    "AG.LND.FRST.ZS",     # Forest area (% of land area)
    "AG.LND.ARBL.ZS",     # Arable land (% of land area)
    "EG.USE.ELEC.KH.PC",  # Electric power consumption (kWh per capita)

    # --- Government & innovation --------------------------------------------
    "GB.XPD.RSDV.GD.ZS",  # Research and development expenditure (% of GDP)
    "IP.PAT.RESD",        # Patent applications, residents
    "MS.MIL.XPND.GD.ZS",  # Military expenditure (% of GDP)
]

# ---------------------------------------------------------------------------
# UN World Population Prospects 2024 (WPP2024), via the Data Portal API
# ---------------------------------------------------------------------------
WPP_YEAR_START = int(os.environ.get("WPP_YEAR_START", 1990))
# Historical-only by default (keeps size down and avoids the multi-variant
# fan-out of future projections). Bump past 2024 and set
# WPP_INCLUDE_PROJECTIONS=1 if you want the Medium-variant projections too.
WPP_YEAR_END = int(os.environ.get("WPP_YEAR_END", 2024))
WPP_INCLUDE_PROJECTIONS = os.environ.get("WPP_INCLUDE_PROJECTIONS", "0") == "1"

# Indicator IDs on the UN Population Division Data Portal API
# (https://population.un.org/dataportalapi/api/v1/indicators?format=csv).
# short_name is only used for logging; the human-readable name comes from
# the API's own indicator metadata, same self-validating principle as WDI.
WPP_INDICATORS = {
    49: "TPopulation",   # Total population by sex (mid-year), thousands
    19: "TFR",           # Total fertility rate (children per woman)
    61: "E0",            # Life expectancy at birth, by sex (years)
    65: "TNetMigration", # Net number of migrants
    66: "TNetMigRT",     # Crude rate of net migration (per 1,000 population)
    55: "CBR",           # Crude birth rate (per 1,000 population)
    59: "CDR",           # Crude death rate (per 1,000 population)
    67: "MedianAgePop",  # Median age of the total population (years)
}

# The Data Portal API returns every projection "Variant" it has (Median,
# High, Low, Constant fertility, ...) once you ask for years past ~2024.
# "Median" is the one continuous series that covers both the historical
# and the medium-projection period, so it's the sane default to keep.
WPP_VARIANT_FILTER = os.environ.get("WPP_VARIANT_FILTER", "Median")

# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", 60))
HTTP_MAX_RETRIES = int(os.environ.get("HTTP_MAX_RETRIES", 4))
HTTP_RETRY_BACKOFF_SECONDS = float(os.environ.get("HTTP_RETRY_BACKOFF_SECONDS", 2.0))

# Chunk size for bulk inserts (rows per executemany/COPY batch).
INSERT_CHUNK_SIZE = int(os.environ.get("INSERT_CHUNK_SIZE", 5000))

# Where fetched raw data is cached on disk, so re-running the script (e.g.
# after fixing a DB connection issue) doesn't re-download everything.
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(__file__), ".cache"))
