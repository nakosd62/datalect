"""
Pure-function tests for backends/base.py's schema-size-limiting helpers:
group_date_sharded_tables, cap_kept_tables, cap_schema_text - plus
normalize_cell_value/fetch_capped_rows, the query-execution row-cap helpers
every DB-API-cursor backend's execute() shares (see EXECUTE_RESULTS_MAX_ROWS's
own docstring for why the cap exists at all). No app/Flask involvement
needed - these are dependency-free over plain data (fetch_capped_rows against
a minimal local fake cursor, not a real driver).
"""

import sys
from datetime import date
from decimal import Decimal

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.base import (
    Backend, group_date_sharded_tables, cap_kept_tables, cap_schema_text,
    normalize_cell_value, fetch_capped_rows,
)


# --- Backend.liveness_sql ----------------------------------------------------
# Regression coverage for the real-world bug that motivated this attribute:
# /api/ping (execute_routes.py) used to be a hardcoded "SELECT 1;" POSTed
# to /api/execute from client.js, which fails against Oracle (no
# SELECT-without-FROM form there - see backends/oracle.py's override).
# Pinning the base class's default here, separately from
# test_oracle_backend.py's override test, so a future backend that forgets
# it exists still inherits a safe, ANSI-valid default rather than silently
# having no liveness_sql at all.

def test_backend_base_class_defaults_liveness_sql_to_select_1():
    assert Backend.liveness_sql == "SELECT 1"


def test_every_registered_backend_except_oracle_and_sheets_uses_the_ansi_select_1_default():
    # Sheets is the second exclusion here (see backends/sheets.py): the
    # GViz query language has no bare "SELECT 1" form either - it has no
    # SELECT/FROM concept at all, just a SELECT clause against an implicit
    # data source, so its liveness_sql is "select * limit 1" instead - a
    # deliberate override, same status as Oracle's.
    from backends import _BACKENDS
    for name, backend_cls in _BACKENDS.items():
        if name in ("oracle", "sheets"):
            continue
        assert backend_cls.liveness_sql == "SELECT 1", (
            f"{name} backend overrides liveness_sql unexpectedly - if that's "
            f"intentional (a dialect that also can't run bare SELECT 1), "
            f"this test's exclusion list needs updating alongside it."
        )


# --- group_date_sharded_tables ---------------------------------------------

def test_no_shards_returns_all_names_unchanged():
    names = ["customers", "orders", "products"]
    kept, groups = group_date_sharded_tables(names)
    assert sorted(kept) == sorted(names)
    assert groups == {}


def test_yyyymmdd_family_collapses_when_over_min_group_size():
    names = [f"events_2024010{i}" for i in range(1, 6)]  # 5 members
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert "events" in groups
    assert groups["events"] == sorted(names)
    # Only the lexicographically-last member survives in `kept`.
    assert kept == [sorted(names)[-1]]


def test_family_below_min_group_size_stays_uncollapsed():
    names = ["reports_20240101", "reports_20240102"]  # only 2
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert groups == {}
    assert sorted(kept) == sorted(names)


def test_supports_yyyymm_yyyy_mm_dd_and_yyyy_us_mm_us_dd_suffixes():
    names = ["m_202401", "m_202402", "m_202403"]
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert "m" in groups

    names2 = ["d_2024-01-01", "d_2024-01-02", "d_2024-01-03"]
    kept2, groups2 = group_date_sharded_tables(names2, min_group_size=3)
    assert "d" in groups2

    names3 = ["d_2024_01_01", "d_2024_01_02", "d_2024_01_03"]
    kept3, groups3 = group_date_sharded_tables(names3, min_group_size=3)
    assert "d" in groups3


def test_prefix_with_its_own_underscores_resolves_via_backtracking():
    names = ["raw_events_20240101", "raw_events_20240102", "raw_events_20240103"]
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert "raw_events" in groups
    assert len(groups["raw_events"]) == 3


def test_mixed_shards_and_plain_tables():
    names = ["customers"] + [f"events_2024010{i}" for i in range(1, 4)]
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert "customers" in kept
    assert "events" in groups


def test_table_name_that_merely_ends_in_a_number_is_not_a_shard_family_of_one():
    # A single table ending in something date-shaped shouldn't be treated
    # as a "family" - min_group_size gates that, not the regex alone.
    kept, groups = group_date_sharded_tables(["events_20240101"], min_group_size=3)
    assert groups == {}
    assert kept == ["events_20240101"]


# --- cap_kept_tables ---------------------------------------------------------

def test_cap_kept_tables_under_limit_is_a_no_op():
    names = ["a", "b", "c"]
    kept, groups, omitted = cap_kept_tables(names, {}, max_tables=10)
    assert kept == ["a", "b", "c"]
    assert omitted == 0


def test_cap_kept_tables_truncates_alphabetically_and_reports_omitted_count():
    names = ["d", "b", "a", "c", "e"]
    kept, groups, omitted = cap_kept_tables(names, {}, max_tables=3)
    assert kept == ["a", "b", "c"]
    assert omitted == 2


def test_cap_kept_tables_drops_shard_group_whose_representative_was_cut():
    # "z_family" collapses to representative "z_9" (alphabetically last),
    # which sorts after the cap and gets cut - the whole group entry must
    # then also disappear from shard_groups, not leave a dangling reference.
    kept_in = ["a", "b", "z_9"]
    shard_groups_in = {"z": ["z_1", "z_9"]}
    kept, groups, omitted = cap_kept_tables(kept_in, shard_groups_in, max_tables=2)
    assert kept == ["a", "b"]
    assert groups == {}


def test_cap_kept_tables_keeps_shard_group_whose_representative_survives():
    kept_in = ["a", "z_9"]
    shard_groups_in = {"z": ["z_1", "z_9"]}
    kept, groups, omitted = cap_kept_tables(kept_in, shard_groups_in, max_tables=5)
    assert groups == shard_groups_in


# --- cap_schema_text ----------------------------------------------------------

def test_cap_schema_text_under_limit_is_unchanged():
    text = "short schema text"
    assert cap_schema_text(text, max_chars=1000) == text


def test_cap_schema_text_empty_or_none_is_unchanged():
    assert cap_schema_text("", max_chars=10) == ""
    assert cap_schema_text(None, max_chars=10) is None


def test_cap_schema_text_truncates_and_appends_note():
    text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
    capped = cap_schema_text(text, max_chars=60)
    assert len(capped) > 60  # note text pushes it back over
    assert "schema truncated" in capped
    assert "C" * 50 not in capped


def test_cap_schema_text_cuts_on_paragraph_boundary_when_possible():
    text = "A" * 30 + "\n\n" + "B" * 100
    capped = cap_schema_text(text, max_chars=50)
    # The cut should land at the \n\n boundary (after the A's), not
    # mid-way through the B's block.
    assert capped.startswith("A" * 30)
    assert not capped.startswith("A" * 30 + "\n\nB" * 5)


def test_cap_schema_text_falls_back_to_hard_cut_when_no_paragraph_boundary():
    text = "A" * 200  # one giant paragraph, no \n\n anywhere
    capped = cap_schema_text(text, max_chars=50)
    assert capped.startswith("A" * 50)
    assert "schema truncated" in capped


# --- normalize_cell_value -----------------------------------------------------

def test_normalize_cell_value_converts_dates_via_isoformat():
    assert normalize_cell_value(date(2024, 1, 15)) == "2024-01-15"


def test_normalize_cell_value_converts_decimal_to_float():
    val = normalize_cell_value(Decimal("19.99"))
    assert val == 19.99
    assert isinstance(val, float)


def test_normalize_cell_value_decodes_bytes_as_utf8():
    assert normalize_cell_value(b"raw-bytes") == "raw-bytes"


def test_normalize_cell_value_replaces_undecodable_bytes_instead_of_raising():
    assert normalize_cell_value(b"\xff\xfe") == "��"


def test_normalize_cell_value_passes_through_plain_values_unchanged():
    assert normalize_cell_value(42) == 42
    assert normalize_cell_value("plain string") == "plain string"
    assert normalize_cell_value(None) is None


def test_normalize_cell_value_catches_a_decimal_like_type_named_decimal_without_to_eng_string():
    # A defensive last resort for a driver-specific decimal type that
    # doesn't happen to implement to_eng_string - see this helper's own
    # docstring. type(val).__name__ == 'Decimal' is what has to catch it.
    class Decimal:  # shadows the real one deliberately, for this one test
        def __init__(self, value):
            self._value = value

        def __float__(self):
            return float(self._value)

    assert normalize_cell_value(Decimal("3.5")) == 3.5


# --- fetch_capped_rows ---------------------------------------------------------
# A minimal local fake, not helpers.FakePgCursor - fetch_capped_rows only
# ever touches .description/.fetchmany()/.fetchone(), so this is deliberately
# simpler than the full scripted-response cursor every backend's own
# execute() test file drives against.

class _FakeCappingCursor:
    def __init__(self, columns, rows):
        self.description = [(c,) for c in columns]
        self._rows = rows
        self._pos = 0

    def fetchmany(self, size):
        chunk = self._rows[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row


def test_fetch_capped_rows_returns_every_row_untruncated_when_under_the_cap():
    cursor = _FakeCappingCursor(["id", "name"], [(1, "Alice"), (2, "Bob")])
    columns, rows, truncated = fetch_capped_rows(cursor, max_rows=10)
    assert columns == ["id", "name"]
    assert rows == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert truncated is False


def test_fetch_capped_rows_stops_at_max_rows_and_flags_truncated():
    all_rows = [(i,) for i in range(10)]
    cursor = _FakeCappingCursor(["n"], all_rows)
    columns, rows, truncated = fetch_capped_rows(cursor, max_rows=3)
    assert rows == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert truncated is True


def test_fetch_capped_rows_exactly_at_the_cap_is_not_truncated():
    # The result set has EXACTLY max_rows rows, not one more - the extra
    # fetchone() check must correctly see nothing left rather than
    # false-flagging this as truncated.
    all_rows = [(i,) for i in range(3)]
    cursor = _FakeCappingCursor(["n"], all_rows)
    columns, rows, truncated = fetch_capped_rows(cursor, max_rows=3)
    assert len(rows) == 3
    assert truncated is False


def test_fetch_capped_rows_never_fetches_more_than_max_rows_plus_one():
    # The whole point of this cap (see EXECUTE_RESULTS_MAX_ROWS's own
    # docstring) is to never pull an unbounded result set into memory -
    # fetchmany(max_rows) plus exactly one fetchone() is the entire fetch
    # footprint regardless of how many millions of rows actually exist
    # server-side beyond the cap.
    class _CountingCursor(_FakeCappingCursor):
        def __init__(self, columns, total_rows):
            super().__init__(columns, [(i,) for i in range(total_rows)])
            self.fetchmany_calls = []

        def fetchmany(self, size):
            self.fetchmany_calls.append(size)
            return super().fetchmany(size)

    cursor = _CountingCursor(["n"], 5_000_000)
    columns, rows, truncated = fetch_capped_rows(cursor, max_rows=100)
    assert len(rows) == 100
    assert truncated is True
    assert cursor.fetchmany_calls == [100]  # exactly one fetchmany call
    assert cursor._pos == 101  # 100 fetched + 1 discarded truncation probe


def test_fetch_capped_rows_applies_normalize_cell_value_to_every_cell():
    cursor = _FakeCappingCursor(["price", "d"], [(Decimal("9.99"), date(2024, 1, 1))])
    columns, rows, truncated = fetch_capped_rows(cursor, max_rows=10)
    assert rows == [{"price": 9.99, "d": "2024-01-01"}]
    assert isinstance(rows[0]["price"], float)
