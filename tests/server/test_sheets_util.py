"""
sheets_util.py's two pure functions: extract_spreadsheet_id() (URL parsing,
bare-id passthrough) and column_letter() (the base-26 spreadsheet-column
algorithm, including the >26-column AA/AB rollover). No app_config.py/
backends import needed for either - see sheets_util.py's own module
docstring for why this module is deliberately dependency-free (import-order
reasons, not just tidiness).
"""

import sys

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from sheets_util import extract_spreadsheet_id, column_letter


# --- extract_spreadsheet_id --------------------------------------------------

def test_extracts_id_from_a_full_edit_url():
    url = "https://docs.google.com/spreadsheets/d/1AbCdEf2345_-xyz/edit#gid=0"
    assert extract_spreadsheet_id(url) == "1AbCdEf2345_-xyz"


def test_extracts_id_from_a_url_with_query_params():
    url = "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit?usp=sharing"
    assert extract_spreadsheet_id(url) == "1AbCdEf2345"


def test_passes_a_bare_id_straight_through():
    assert extract_spreadsheet_id("1AbCdEf2345_-xyz") == "1AbCdEf2345_-xyz"


def test_strips_surrounding_whitespace():
    assert extract_spreadsheet_id("  1AbCdEf2345  ") == "1AbCdEf2345"


def test_returns_none_for_blank_input():
    assert extract_spreadsheet_id("") is None
    assert extract_spreadsheet_id(None) is None
    assert extract_spreadsheet_id("   ") is None


def test_returns_none_for_an_unparseable_url():
    assert extract_spreadsheet_id("https://example.com/not-a-sheets-url") is None


# --- column_letter -----------------------------------------------------------

def test_column_letter_single_letters():
    assert column_letter(1) == "A"
    assert column_letter(2) == "B"
    assert column_letter(26) == "Z"


def test_column_letter_double_letter_rollover():
    assert column_letter(27) == "AA"
    assert column_letter(28) == "AB"
    assert column_letter(52) == "AZ"
    assert column_letter(53) == "BA"


def test_column_letter_beyond_26_columns_sample():
    # Sanity check across a >26-column schema - every position gets a
    # distinct, correctly-ordered letter, not just the two boundary cases
    # above.
    letters = [column_letter(i) for i in range(1, 30)]
    assert letters[24] == "Y"
    assert letters[25] == "Z"
    assert letters[26] == "AA"
    assert letters[27] == "AB"
    assert letters[28] == "AC"
