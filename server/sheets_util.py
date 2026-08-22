"""
sheets_util.py

Tiny, dependency-free (stdlib `re` only) helpers shared by the Google
Sheets ("gviz") dialect. Deliberately NOT part of backends/sheets.py:
app_config.py (which builds CONFIGURED_DBS, including any Sheets presets)
is imported before anything backend-related and today has zero dependency
on the backends/ package - importing anything from backends.sheets would
force Python to run backends/__init__.py first (a package's __init__.py
always runs before any of its submodules), which imports every other
backend's driver library (google-cloud-bigquery, snowflake-connector-python,
databricks-sql-connector, oracledb, python-tds, ...) during app_config.py's
own import, for no functional benefit. This module sits below app_config.py,
config_routes.py, and backends/sheets.py alike, with nothing to pull in but
`re`, so all three can import it directly with zero new import-ordering
coupling.
"""

import re

_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_BARE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def extract_spreadsheet_id(url_or_id):
    """Pulls a spreadsheet id out of a pasted Google Sheets URL (any of the
    usual forms - .../spreadsheets/d/<id>/edit#gid=0, .../edit?usp=sharing,
    etc. - all share the same /spreadsheets/d/<id> segment), or passes a
    bare id straight through unchanged if that's what was given instead.
    Returns None if neither shape matches (e.g. blank input, or a URL that
    isn't a spreadsheets/d/... link at all)."""
    text = (url_or_id or "").strip()
    if not text:
        return None
    m = _SPREADSHEET_URL_RE.search(text)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(text):
        return text
    return None


def column_letter(position):
    """1-indexed spreadsheet column letter, the same algorithm Excel/Sheets
    themselves use (position=1 -> "A", 26 -> "Z", 27 -> "AA", ...) - needed
    because the GViz query language addresses columns positionally, by
    spreadsheet letter, never by header/label text."""
    letters = ""
    n = position
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters
