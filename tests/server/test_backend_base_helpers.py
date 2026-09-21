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
    schema_text_was_truncated, schema_text_has_omitted_tables,
    find_naming_convention_relationships,
    normalize_cell_value, fetch_capped_rows,
    resolve_timeout_seconds,
    format_bytes_human, format_compact_count, format_dataset_size_line,
    parse_dataset_size_line, quantize_schema_size_tokens,
    format_multiline_schema_entry_body,
    extract_entry_names_from_schema_text, split_schema_text_into_entries,
    derive_tables_only_schema_text,
    _strip_trailing_asides,
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


def test_bare_year_suffixed_family_collapses():
    # e.g. a BigQuery dataset partitioned one table per year -
    # pageviews_2015 .. pageviews_2026 - rather than the more granular
    # YYYYMMDD/YYYYMM shapes covered above.
    names = [f"pageviews_{y}" for y in range(2015, 2027)]  # 12 members
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert "pageviews" in groups
    assert groups["pageviews"] == sorted(names)
    assert kept == [sorted(names)[-1]]  # "pageviews_2026"


def test_bare_year_suffix_is_constrained_to_1900_2099():
    # Not an unconstrained \d{4} - a bare 4-digit suffix is far more likely
    # than YYYYMM/YYYYMMDD to collide with an unrelated numeric ID a table
    # family just happens to share (e.g. sequential batch/chunk numbers)
    # rather than an actual year, so only 19xx/20xx is treated as a year.
    names = ["chunk_0001", "chunk_0002", "chunk_0003"]
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert groups == {}
    assert sorted(kept) == sorted(names)

    # A year-shaped 4-digit suffix outside 1900-2099 (e.g. a made-up/far-
    # future placeholder) is likewise left uncollapsed.
    names2 = ["events_3015", "events_3016", "events_3017"]
    kept2, groups2 = group_date_sharded_tables(names2, min_group_size=3)
    assert groups2 == {}
    assert sorted(kept2) == sorted(names2)


def test_supports_postgres_declarative_partitioning_p_prefixed_suffix():
    # e.g. "payment_p2023_10" - Postgres declarative partitioning's own
    # common monthly-range-partition naming convention (a literal "p"
    # followed by zero-padded year_month), distinct from the plain
    # all-digit YYYYMM/YYYY_MM_DD suffixes covered above.
    names = ["payment_p2023_10", "payment_p2023_11", "payment_p2023_12"]
    kept, groups = group_date_sharded_tables(names, min_group_size=3)
    assert "payment" in groups
    assert groups["payment"] == sorted(names)
    assert kept == [sorted(names)[-1]]


def test_p_prefixed_suffix_month_must_be_two_digits_to_sort_chronologically():
    # A lone single-digit month (no leading zero) wouldn't match the
    # pattern at all - it's kept as a plain table rather than silently
    # joining a family where alphabetical sort could misorder it.
    kept, groups = group_date_sharded_tables(
        ["payment_p2023_9", "payment_p2023_10", "payment_p2023_11"],
        min_group_size=3,
    )
    assert groups == {}
    assert sorted(kept) == sorted(["payment_p2023_9", "payment_p2023_10", "payment_p2023_11"])


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


# --- schema_text_was_truncated -------------------------------------------------
#
# Regression guard for db.py's own truncation-visibility logging (see
# _fetch_database_schema): this is what recognizes a cap_schema_text()
# result that actually got cut, without re-running cap_schema_text or
# needing the original uncapped text - so these tests pin down that it
# reads cap_schema_text's own output correctly, in both directions.

def test_schema_text_was_truncated_true_for_an_actually_truncated_result():
    text = "A" * 200
    capped = cap_schema_text(text, max_chars=50)
    assert schema_text_was_truncated(capped) is True


def test_schema_text_was_truncated_false_for_untruncated_text():
    text = "short schema text"
    assert schema_text_was_truncated(cap_schema_text(text, max_chars=1000)) is False
    assert schema_text_was_truncated(text) is False  # never even passed through cap_schema_text


def test_schema_text_was_truncated_false_for_none_or_empty():
    assert schema_text_was_truncated(None) is False
    assert schema_text_was_truncated("") is False


# --- schema_text_has_omitted_tables --------------------------------------------
#
# Companion to schema_text_was_truncated above, for the OTHER cap
# (SCHEMA_MAX_TABLES rather than SCHEMA_MAX_CHARS): recognizes the "N more
# table(s)... not shown" note every backend's own `if omitted_count:` block
# appends (see e.g. backends/postgres.py) via the literal substring both
# that wording and mongodb_sql.py's slightly different one share, rather
# than needing the original omitted_count value.

def test_schema_text_has_omitted_tables_true_when_the_note_is_present():
    text = (
        "Table: a\n  id integer NOT NULL\n\n"
        "[... 5 more table(s)/table-family(ies) not shown - this schema has "
        "more than the 200-table summary limit. Ask about a narrower set of "
        "tables to see the rest.]"
    )
    assert schema_text_has_omitted_tables(text) is True


def test_schema_text_has_omitted_tables_true_for_mongodbs_own_wording():
    # mongodb_sql.py's note omits "/table-family(ies)" (no date-shard
    # families for collections) - still recognized via the shared
    # "more table(s)" substring both wordings contain.
    text = (
        "[... 3 more table(s) not shown - this schema has more than the "
        "200-table summary limit. Ask about a narrower set of collections "
        "to see the rest.]"
    )
    assert schema_text_has_omitted_tables(text) is True


def test_schema_text_has_omitted_tables_false_when_every_table_was_described():
    text = "Table: a\n  id integer NOT NULL\n\nTable: b\n  id integer NOT NULL"
    assert schema_text_has_omitted_tables(text) is False


def test_schema_text_has_omitted_tables_false_for_none_or_empty():
    assert schema_text_has_omitted_tables(None) is False
    assert schema_text_has_omitted_tables("") is False


# --- find_naming_convention_relationships --------------------------------------
#
# Shared Phase 2 heuristic (see its own docstring in backends/base.py):
# every backend's get_schema() calls this same function rather than
# reimplementing the naming-convention pass per dialect, so it's tested
# once here rather than once per backend.

def test_finds_prefix_match_against_a_real_table_name():
    matches = find_naming_convention_relationships({
        "orders": ["id", "customer_id", "total"],
        "customers": ["id", "name"],
    })
    assert len(matches) == 1
    assert "orders.customer_id" in matches[0]
    assert "customers" in matches[0]
    assert "likely relationship (unconfirmed)" in matches[0]


def test_finds_shared_fk_shaped_column_name_across_tables_with_no_matching_table_name():
    # Neither table is named "regions" - only heuristic 2 (same column
    # name in 2+ tables) should catch this, not heuristic 1.
    matches = find_naming_convention_relationships({
        "stores": ["id", "region_id"],
        "warehouses": ["id", "region_id"],
    })
    texts = "\n".join(matches)
    assert "stores.region_id" in texts
    assert "warehouses.region_id" in texts


def test_bare_id_column_is_never_flagged_as_a_relationship():
    # Regression guard: virtually every table has its own "id" primary
    # key, so two (or ten) tables all having a plain "id" column must
    # never be reported as a "likely relationship" - that would be true
    # of almost any schema and would just be noise, not a real signal.
    matches = find_naming_convention_relationships({
        "orders": ["id", "total"],
        "customers": ["id", "name"],
        "products": ["id", "name"],
    })
    assert matches == []


def test_bare_underscore_id_column_is_never_flagged_either():
    # Same regression guard as above, for MongoDB's own universal
    # per-document "_id" field - a bare suffix with no real prefix before
    # it, same reasoning as plain "id".
    matches = find_naming_convention_relationships({
        "orders": ["_id", "total"],
        "customers": ["_id", "name"],
    })
    assert matches == []


def test_no_false_positive_for_an_unrelated_table():
    matches = find_naming_convention_relationships({
        "orders": ["id", "customer_id"],
        "customers": ["id"],
        "widgets": ["id", "name"],
    })
    texts = "\n".join(matches)
    assert "widgets" not in texts


def test_empty_or_none_input_returns_empty_list():
    assert find_naming_convention_relationships({}) == []
    assert find_naming_convention_relationships(None) == []


def test_deterministic_order_regardless_of_dict_iteration_order():
    table_columns = {
        "warehouses": ["id", "region_id"],
        "stores": ["id", "region_id"],
    }
    matches_a = find_naming_convention_relationships(table_columns)
    matches_b = find_naming_convention_relationships(
        {"stores": table_columns["stores"], "warehouses": table_columns["warehouses"]}
    )
    assert matches_a == matches_b


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


# --- resolve_timeout_seconds() -----------------------------------------------
# Per-dataset overrides for the two app-wide connection-behavior timeouts
# (backends/base.py's own DB_CONNECT_TIMEOUT_SECONDS, execute_routes.py's
# SQL_EXECUTE_TIMEOUT_SECONDS) - see that function's own docstring for the
# full reasoning. Pure function, no I/O - every case here is exercised
# directly against a plain dict descriptor.

def test_resolve_timeout_seconds_falls_back_to_default_when_field_missing():
    assert resolve_timeout_seconds({"type": "postgres"}, "connect_timeout_seconds", 10) == 10


def test_resolve_timeout_seconds_falls_back_to_default_for_none_descriptor():
    assert resolve_timeout_seconds(None, "connect_timeout_seconds", 10) == 10


def test_resolve_timeout_seconds_falls_back_to_default_for_blank_string():
    assert resolve_timeout_seconds({"connect_timeout_seconds": ""}, "connect_timeout_seconds", 10) == 10


def test_resolve_timeout_seconds_uses_a_valid_positive_override():
    assert resolve_timeout_seconds({"connect_timeout_seconds": 25}, "connect_timeout_seconds", 10) == 25


def test_resolve_timeout_seconds_accepts_a_numeric_string_override():
    # A hand-edited DATABASE_PRESETS_FILE entry, or a custom-connection
    # payload field that happened to arrive as a string rather than a
    # JSON number - both should resolve the same way a real number does.
    assert resolve_timeout_seconds({"execute_timeout_seconds": "45"}, "execute_timeout_seconds", 30) == 45.0


def test_resolve_timeout_seconds_falls_back_to_default_for_non_numeric_override():
    assert resolve_timeout_seconds({"connect_timeout_seconds": "soon"}, "connect_timeout_seconds", 10) == 10


def test_resolve_timeout_seconds_falls_back_to_default_for_zero_override():
    # Unlike SQL_EXECUTE_TIMEOUT_SECONDS's own env-var-level "0 disables
    # the timeout" convention, a per-dataset override of 0/negative isn't
    # "disable it for this dataset" - it's treated as unset, same as a
    # blank/missing value.
    assert resolve_timeout_seconds({"execute_timeout_seconds": 0}, "execute_timeout_seconds", 30) == 30


def test_resolve_timeout_seconds_falls_back_to_default_for_negative_override():
    assert resolve_timeout_seconds({"connect_timeout_seconds": -5}, "connect_timeout_seconds", 10) == 10


def test_resolve_timeout_seconds_reads_only_the_requested_field_name():
    descriptor = {"connect_timeout_seconds": 25, "execute_timeout_seconds": 90}
    assert resolve_timeout_seconds(descriptor, "connect_timeout_seconds", 10) == 25
    assert resolve_timeout_seconds(descriptor, "execute_timeout_seconds", 30) == 90


# --- format_bytes_human() ----------------------------------------------------
# Pure formatting helper each SQL backend's format_dataset_size_line() (see
# below) leans on for the trailing "(~X estimated storage)" parenthetical -
# never used for anything byte-exact.

def test_format_bytes_human_returns_empty_string_for_none():
    assert format_bytes_human(None) == ""


def test_format_bytes_human_returns_empty_string_for_negative():
    assert format_bytes_human(-1) == ""


def test_format_bytes_human_formats_plain_bytes_with_no_decimal():
    assert format_bytes_human(0) == "0 bytes"
    assert format_bytes_human(500) == "500 bytes"
    assert format_bytes_human(1023) == "1023 bytes"


def test_format_bytes_human_kb_boundary():
    # Exactly 1024 bytes crosses into KB (1024 is NOT "< 1024.0", so it
    # rolls over rather than staying "1024 bytes").
    assert format_bytes_human(1024) == "1.0 KB"
    assert format_bytes_human(1536) == "1.5 KB"


def test_format_bytes_human_mb_boundary():
    assert format_bytes_human(1024 * 1024) == "1.0 MB"


def test_format_bytes_human_gb_boundary():
    assert format_bytes_human(1024 ** 3) == "1.0 GB"


def test_format_bytes_human_tb_boundary():
    assert format_bytes_human(1024 ** 4) == "1.0 TB"


def test_format_bytes_human_pb_is_the_terminal_unit():
    # PB is the last unit in the ladder - the loop's "or unit == 'PB'"
    # guard means it always returns here, however large the value.
    assert format_bytes_human(5 * 1024 ** 5) == "5.0 PB"


# --- format_compact_count() --------------------------------------------------
# Shortens a large row count to K/M/B with 3 significant digits - used by
# format_dataset_size_line() below for its own row-count figure, and mirrored
# exactly by client.js's formatCompactCount() for the Schema Viewer's
# per-table row-count suffix (see that function's own comment).

def test_format_compact_count_stays_plain_below_1000():
    assert format_compact_count(0) == "0"
    assert format_compact_count(999) == "999"
    assert format_compact_count(1234) != "1234"  # sanity: the boundary above is real


def test_format_compact_count_k_boundary_and_precision():
    assert format_compact_count(1000) == "1.00K"
    assert format_compact_count(1234) == "1.23K"
    assert format_compact_count(12345) == "12.3K"
    assert format_compact_count(123456) == "123K"


def test_format_compact_count_m_boundary_and_precision():
    assert format_compact_count(1_000_000) == "1.00M"
    assert format_compact_count(1_234_567) == "1.23M"
    assert format_compact_count(12_345_678) == "12.3M"
    assert format_compact_count(123_456_789) == "123M"


def test_format_compact_count_b_is_the_terminal_unit():
    assert format_compact_count(1_000_000_000) == "1.00B"
    assert format_compact_count(8_500_000_000) == "8.50B"
    # No further unit past B - a trillion-row figure would never occur in
    # practice, but this still stays 3-significant-digit-accurate rather
    # than raising or falling back to a raw, ungrouped number.
    assert format_compact_count(1_234_000_000_000) == "1234B"


def test_format_compact_count_handles_a_non_numeric_input_without_raising():
    assert format_compact_count(None) == "None"
    assert format_compact_count("not a number") == "not a number"


# --- format_multiline_schema_entry_body() ------------------------------------
# Reindents a View/Routine definitions entry's body (a view's SELECT text or a
# routine's CREATE.../body text, straight from the database) so every line
# after the first is guaranteed to start indented - see that function's own
# docstring in backends/base.py for the two independent webClient parsing
# failure modes this prevents (an entry silently truncated to its own first
# line, or a whole "View definitions:"/"Routine definitions:" section
# silently truncated by one badly-indented body inside it).

def test_format_multiline_schema_entry_body_leaves_a_single_line_body_untouched():
    assert format_multiline_schema_entry_body("SELECT 1 FROM orders") == "SELECT 1 FROM orders"


def test_format_multiline_schema_entry_body_indents_every_continuation_line():
    raw = "SELECT a.id,\na.name\nFROM a;"
    assert format_multiline_schema_entry_body(raw) == "SELECT a.id,\n    a.name\n    FROM a;"


def test_format_multiline_schema_entry_body_adds_onto_indentation_the_database_already_used():
    # A dialect's own pretty-printer may already indent continuation lines
    # by varying amounts to show real nesting (e.g. Postgres's
    # pg_get_viewdef) - the four guaranteed spaces are ADDED on top of
    # whatever was already there, not a flat replacement, so that relative
    # structure survives; only the GUARANTEE (every continuation line
    # starts with at least four spaces, whatever it started with) is new.
    raw = "SELECT a.id,\n   a.name\n  FROM a;"
    assert format_multiline_schema_entry_body(raw) == "SELECT a.id,\n       a.name\n      FROM a;"


def test_format_multiline_schema_entry_body_strips_outer_whitespace_first():
    raw = "\n  SELECT 1;\n  "
    assert format_multiline_schema_entry_body(raw) == "SELECT 1;"


def test_format_multiline_schema_entry_body_returns_empty_string_for_none_or_blank():
    assert format_multiline_schema_entry_body(None) == ""
    assert format_multiline_schema_entry_body("") == ""
    assert format_multiline_schema_entry_body("   ") == ""


# --- format_dataset_size_line() ----------------------------------------------
# Assembles the single "Estimated dataset size: ..." line each SQL backend's
# get_schema() (deep) appends to its schema text - see backends/base.py's own
# docstring for the full contract. Deliberately just one headline figure - a
# byte-size estimate when available, never both a row count AND a table count
# alongside it (a caller still computes those for its own "did this query
# actually find anything" gating, but this function doesn't render them).

def test_format_dataset_size_line_returns_empty_string_when_both_totals_none():
    assert format_dataset_size_line() == ""
    assert format_dataset_size_line(note="ignored") == ""


def test_format_dataset_size_line_prefers_bytes_over_rows_when_both_are_given():
    # Rows are only ever shown when there's no byte figure at all (see the
    # next test) - here, despite total_rows also being passed, the rendered
    # line is bytes-only.
    assert (
        format_dataset_size_line(total_rows=1234567, total_bytes=3_400_000_000)
        == "Estimated dataset size: ~3.2 GB"
    )


def test_format_dataset_size_line_falls_back_to_a_compact_row_count_with_no_bytes():
    # No byte figure at all (e.g. backends/databricks.py, or a byte query
    # that failed for a dialect that normally has one) - 1,234,567 shortens
    # to "1.23M" via format_compact_count() (see that function's own tests
    # above for the K/M/B ladder in isolation).
    assert (
        format_dataset_size_line(total_rows=1234567)
        == "Estimated dataset size: ~1.23M rows"
    )


def test_format_dataset_size_line_note_appears_as_trailing_parenthetical():
    assert (
        format_dataset_size_line(total_rows=100, note="stats may be stale")
        == "Estimated dataset size: ~100 rows (stats may be stale)"
    )
    # Same trailing-parenthetical treatment for the bytes-preferred case.
    assert (
        format_dataset_size_line(total_bytes=2048, note="caveat text")
        == "Estimated dataset size: ~2.0 KB (caveat text)"
    )


# --- parse_dataset_size_line() -----------------------------------------------
# The reciprocal of format_dataset_size_line() above - pulls that same line's
# own value back out of a full schema text (see db.py's
# build_group_schema_summaries(), the dataset-group Schema Viewer's own
# "Data Size" column). Mirrors client.js's parseSchemaDatasetSizeLine()
# exactly (same anchor, multiline, first match only).

def test_parse_dataset_size_line_extracts_the_value_after_the_label():
    assert parse_dataset_size_line("Estimated dataset size: ~2.4 GB") == "~2.4 GB"


def test_parse_dataset_size_line_finds_the_line_anywhere_in_a_multiline_schema():
    schema = (
        "Table: deals\n  id integer NOT NULL\n\n"
        "Session: timezone=UTC\n\n"
        "Estimated dataset size: ~1.23M rows"
    )
    assert parse_dataset_size_line(schema) == "~1.23M rows"


def test_parse_dataset_size_line_returns_none_when_no_such_line_exists():
    assert parse_dataset_size_line("Table: deals\n  id integer NOT NULL\n") is None
    assert parse_dataset_size_line("") is None
    assert parse_dataset_size_line(None) is None


def test_parse_dataset_size_line_only_matches_the_first_occurrence():
    # Should never happen for a real schema text (each connection's deep
    # fetch appends this line at most once) - documented here purely to
    # pin down the "first match wins" behavior rather than leaving it
    # implicit.
    schema = "Estimated dataset size: ~1.0 GB\nEstimated dataset size: ~2.0 GB"
    assert parse_dataset_size_line(schema) == "~1.0 GB"


# --- quantize_schema_size_tokens() -------------------------------------------
# Shared by db.py's build_group_schema_summaries() (the dataset-group Schema
# Viewer's own "Schema Size" column) and client.js's own facts-line figure
# (SCHEMA_VIEWER_TOKEN_QUANTUM there) - both round the raw chars-per-token
# estimate UP to the nearest 100, per an explicit request, since it's only
# ever a rough cost estimate.

def test_quantize_schema_size_tokens_rounds_up_to_the_next_hundred():
    assert quantize_schema_size_tokens(101) == 200
    assert quantize_schema_size_tokens(199) == 200
    assert quantize_schema_size_tokens(250.4) == 300


def test_quantize_schema_size_tokens_leaves_an_exact_multiple_of_100_unchanged():
    assert quantize_schema_size_tokens(100) == 100
    assert quantize_schema_size_tokens(200) == 200


def test_quantize_schema_size_tokens_floors_any_positive_value_at_100():
    # A schema small enough to raw-estimate under 100 tokens still shows as
    # "100 tokens" (the quantization floor), never "0 tokens" - "some
    # tokens, but a small amount" is honest; a bare 0 for a real,
    # non-empty schema would read as an error.
    assert quantize_schema_size_tokens(1) == 100
    assert quantize_schema_size_tokens(0.4) == 100


def test_quantize_schema_size_tokens_returns_zero_for_zero_or_negative_input():
    assert quantize_schema_size_tokens(0) == 0
    assert quantize_schema_size_tokens(-5) == 0


# --- _strip_trailing_asides() ------------------------------------------------
# Regression coverage for two real, separate bugs that both showed up as
# the same symptom (a shard family's Schema Viewer row showing an entire
# multi-clause sentence instead of its bare wildcard pattern), fixed one
# after the other:
#
# Bug 1 - nested parens: this used to be a single non-nesting regex
# (r'\s*\([^)]*\)\s*$'), which can never match a descriptive parenthetical
# that itself contains a nested, balanced parenthetical - `[^)]*` can't
# cross the inner ")" to reach the real outer one, so the whole match
# silently fails and NOTHING gets stripped. BigQuery's own "Table family"
# heading has exactly this shape (a "(e.g. WHERE _TABLE_SUFFIX BETWEEN
# '...' AND '...')" aside inside the outer descriptive parenthetical -
# see backends/bigquery.py's get_schema()). Fixed by walking the string
# from the end and counting paren depth instead of using a regex at all
# (the function was named _strip_trailing_parenthetical at this point).
#
# Bug 2 - a bracket annotation chained AFTER the parenthetical: fixing
# bug 1 wasn't enough - a BigQuery table (or family) that's also flagged
# external and/or require_partition_filter gets a further
# " [external table; REQUIRES PARTITION FILTER on <col>]" appended onto
# the SAME heading line, after its closing ")" (see get_schema()'s own
# heading_annotations handling). Since that bracket, not a paren, is now
# the very last character, the depth-walk from bug 1's fix bailed out
# immediately (`stripped.endswith(")")` was False) and stripped nothing
# at all - the exact same "whole sentence becomes the name" symptom,
# just from a different cause, and the reason a user still saw the ugly
# Wikipedia pageviews_* row after bug 1's fix had already shipped and a
# fresh schema refetch had run. Fixed by renaming this function to
# _strip_trailing_asides and generalizing it to strip EITHER bracket type
# ("(...)" or "[...]"), repeating until neither remains, so a "(...)"
# immediately followed by a "[...]" (today's real BigQuery shape) is
# fully stripped down to the bare wildcard, not just partially.

def test_strip_trailing_asides_removes_a_simple_trailing_aside():
    assert _strip_trailing_asides("events (partitioned by day)") == "events"


def test_strip_trailing_asides_removes_a_parenthetical_containing_a_nested_one():
    text = (
        "`proj.ds.pageviews_*` (12 date-sharded tables, e.g. pageviews_2015 "
        ".. pageviews_2026; filter via _TABLE_SUFFIX (e.g. WHERE "
        "_TABLE_SUFFIX BETWEEN '...' AND '...'); never query a single "
        "literal date-suffixed table name from this family)"
    )
    assert _strip_trailing_asides(text) == "`proj.ds.pageviews_*`"


def test_strip_trailing_asides_handles_multiple_levels_of_nesting():
    assert _strip_trailing_asides("name (a (b (c) d) e)") == "name"


def test_strip_trailing_asides_leaves_a_heading_with_no_aside_untouched():
    assert _strip_trailing_asides("customers") == "customers"


def test_strip_trailing_asides_leaves_unbalanced_parens_untouched():
    # More closes than opens - can't identify a matching outer "(" to cut
    # from, so this degrades to a no-op rather than guessing wrong.
    assert _strip_trailing_asides("odd) trailing) parens)") == "odd) trailing) parens)"


def test_strip_trailing_asides_ignores_a_parenthetical_not_at_the_very_end():
    # Only a TRAILING aside is stripped - one that happens to appear
    # mid-string, with real content after it, is left alone.
    assert _strip_trailing_asides("a (b) c") == "a (b) c"


def test_strip_trailing_asides_strips_a_lone_bracket_annotation_with_no_parenthetical():
    assert _strip_trailing_asides("orders [external table]") == "orders"


def test_strip_trailing_asides_strips_a_parenthetical_followed_by_a_bracket_annotation():
    # The real bug 2 shape: BigQuery's heading_annotations bracket is
    # appended AFTER the descriptive parenthetical's own closing ")",
    # so both have to come off, in that order, to reach the bare name.
    text = "`proj.ds.pageviews_*` (12 date-sharded tables, e.g. a .. b) [REQUIRES PARTITION FILTER on datehour]"
    assert _strip_trailing_asides(text) == "`proj.ds.pageviews_*`"


def test_strip_trailing_asides_strips_a_nested_parenthetical_followed_by_a_bracket_annotation():
    # bug 1 and bug 2's fixes chained together on one real-world heading:
    # a nested "(e.g. ...)" aside inside the outer parenthetical, THEN a
    # bracket annotation after that - the exact shape of the Wikipedia
    # pageviews_* family (require_partition_filter on datehour) that
    # motivated both fixes.
    text = (
        "`bigquery-public-data.wikipedia.pageviews_*` (12 date-sharded "
        "tables, e.g. pageviews_2015 .. pageviews_2026; identical columns "
        "in every member - query this family with the wildcard form "
        "`bigquery-public-data.wikipedia.pageviews_*`, filtering/"
        "identifying the shard via the _TABLE_SUFFIX pseudo-column (e.g. "
        "WHERE _TABLE_SUFFIX BETWEEN '...' AND '...'); never query a "
        "single literal date-suffixed table name from this family) "
        "[REQUIRES PARTITION FILTER on datehour]"
    )
    assert _strip_trailing_asides(text) == "`bigquery-public-data.wikipedia.pageviews_*`"


def test_strip_trailing_asides_strips_multiple_semicolon_joined_annotations_in_one_bracket():
    # get_schema() joins several annotations into ONE bracket
    # ("[external table; REQUIRES PARTITION FILTER on ts]"), not one
    # bracket per annotation - still just a single trailing aside to strip.
    text = "`p.d.t_*` (3 date-sharded tables, e.g. t_2020 .. t_2022) [external table; REQUIRES PARTITION FILTER on ts]"
    assert _strip_trailing_asides(text) == "`p.d.t_*`"


# --- extract_entry_names_from_schema_text(): the nested-parenthetical /
# bracket-annotation cases -----------------------------------------------
# (see test_connection_router.py for this function's broader coverage
# against every known heading convention - this is specifically the two
# regression cases _strip_trailing_asides above exists to fix)

def test_extract_entry_names_handles_a_table_family_heading_with_nested_parens():
    schema = (
        "Table family: `proj.ds.pageviews_*` (12 date-sharded tables, e.g. "
        "pageviews_2015 .. pageviews_2026; filter via _TABLE_SUFFIX (e.g. "
        "WHERE _TABLE_SUFFIX BETWEEN '...' AND '...'); never query a single "
        "literal date-suffixed table name from this family)\n"
        "  id INTEGER NOT NULL\n"
    )
    assert extract_entry_names_from_schema_text(schema) == ["`proj.ds.pageviews_*`"]


def test_extract_entry_names_handles_a_table_family_heading_with_a_trailing_bracket_annotation():
    schema = (
        "Table family: `proj.ds.pageviews_*` (12 date-sharded tables, e.g. "
        "pageviews_2015 .. pageviews_2026) [REQUIRES PARTITION FILTER on datehour]\n"
        "  datehour TIMESTAMP NOT NULL\n"
    )
    assert extract_entry_names_from_schema_text(schema) == ["`proj.ds.pageviews_*`"]


# --- split_schema_text_into_entries() ----------------------------------------
# Backs config_routes.py's GET /api/schema (the Schema Viewer feature) -
# unlike extract_entry_names_from_schema_text, this also needs each entry's
# own full "heading" (parenthetical included, for the LLM-facing raw-text
# pane) and "text" block, alongside the same stripped "name" a UI list row
# and the ER diagram key off of (see backends/base.py's own docstring on
# the blast radius of "name" vs "heading"/"text").

def test_split_schema_text_into_entries_plain_table_heading():
    schema = "Table: customers\n  id integer NOT NULL\n  name text NOT NULL\n"
    entries = split_schema_text_into_entries(schema)
    assert len(entries) == 1
    assert entries[0]["name"] == "customers"
    assert entries[0]["heading"] == "Table: customers"
    assert entries[0]["text"] == schema.rstrip("\n")


def test_split_schema_text_into_entries_multiple_plain_tables():
    schema = "Table: customers\n  id integer NOT NULL\n\nTable: orders\n  id integer NOT NULL\n"
    entries = split_schema_text_into_entries(schema)
    assert [e["name"] for e in entries] == ["customers", "orders"]
    assert entries[0]["text"] == "Table: customers\n  id integer NOT NULL"
    assert entries[1]["text"] == "Table: orders\n  id integer NOT NULL"


def test_split_schema_text_into_entries_table_family_name_is_the_bare_wildcard_not_the_full_sentence():
    # Bug 1 (see _strip_trailing_asides' own comment above): before that
    # fix existed, "name" here came back as the ENTIRE heading tail
    # (wildcard + full multi-clause description) because the nested "(e.g.
    # WHERE ...)" aside broke the old non-nesting regex - this is what made
    # the Schema Viewer's table list row unusably long for a BigQuery
    # date-sharded family. "heading" and "text" (the LLM-facing content)
    # are untouched either way - only "name" (the UI-facing label) is
    # affected.
    schema = (
        "Table family: `bigquery-public-data.wikipedia.pageviews_*` "
        "(12 date-sharded tables, e.g. pageviews_2015 .. pageviews_2026; "
        "identical columns in every member - query this family with the "
        "wildcard form `bigquery-public-data.wikipedia.pageviews_*`, "
        "filtering/identifying the shard via the _TABLE_SUFFIX "
        "pseudo-column (e.g. WHERE _TABLE_SUFFIX BETWEEN '...' AND "
        "'...'); never query a single literal date-suffixed table name "
        "from this family)\n"
        "  datehour TIMESTAMP NOT NULL\n"
        "  title STRING NOT NULL\n"
        "  views INTEGER NOT NULL\n"
    )
    entries = split_schema_text_into_entries(schema)
    assert len(entries) == 1
    assert entries[0]["name"] == "`bigquery-public-data.wikipedia.pageviews_*`"
    # The full descriptive heading is still there, verbatim, for the
    # raw-text pane and the LLM-facing schema text - only the derived
    # "name" field (the UI list row's label) was ever wrong.
    assert entries[0]["heading"].startswith("Table family: `bigquery-public-data.wikipedia.pageviews_*` (12 date-sharded")
    assert "never query a single literal date-suffixed table name from this family)" in entries[0]["text"]
    assert "datehour TIMESTAMP NOT NULL" in entries[0]["text"]


def test_split_schema_text_into_entries_table_family_name_strips_a_trailing_require_partition_filter_annotation_too():
    # Bug 2 (see _strip_trailing_asides' own comment above): fixing bug 1
    # wasn't enough on its own. This was the real-world Wikipedia
    # pageviews_* heading (require_partition_filter=true on `datehour` -
    # see backends/bigquery.py's get_schema()) at the time, which chains a
    # "[REQUIRES PARTITION FILTER on datehour]" bracket annotation onto
    # the heading AFTER its closing ")" - a real user still saw the whole
    # ugly sentence as the Schema Viewer's table name even after bug 1's
    # fix had already shipped and a fresh schema refetch had run, because
    # the depth-walk back then only recognized a trailing ")", not "]".
    # (get_schema() has since ALSO started using a bare leading pattern
    # here instead of this fully-qualified one - see the next test - but
    # this fully-qualified/backtick-quoted shape is still worth its own
    # coverage: it's a strictly harder input for the stripping walk than
    # a bare pattern is, and nothing about the walk cares what precedes
    # the parenthetical it's stripping from the end.)
    schema = (
        "Table family: `bigquery-public-data.wikipedia.pageviews_*` "
        "(12 date-sharded tables, e.g. pageviews_2015 .. pageviews_2026; "
        "identical columns in every member - query this family with the "
        "wildcard form `bigquery-public-data.wikipedia.pageviews_*`, "
        "filtering/identifying the shard via the _TABLE_SUFFIX "
        "pseudo-column (e.g. WHERE _TABLE_SUFFIX BETWEEN '...' AND "
        "'...'); never query a single literal date-suffixed table name "
        "from this family) [REQUIRES PARTITION FILTER on datehour]\n"
        "  datehour TIMESTAMP NOT NULL\n"
        "  title STRING NOT NULL\n"
        "  views INTEGER NOT NULL\n"
    )
    entries = split_schema_text_into_entries(schema)
    assert len(entries) == 1
    assert entries[0]["name"] == "`bigquery-public-data.wikipedia.pageviews_*`"
    # The bracket annotation (and the rest of the descriptive heading) is
    # still there, verbatim, in "heading"/"text" - the LLM (and the Schema
    # Viewer's own raw-text pane) still needs to know this family requires
    # a partition filter; only the derived "name" strips it.
    assert entries[0]["heading"].endswith("[REQUIRES PARTITION FILTER on datehour]")
    assert "[REQUIRES PARTITION FILTER on datehour]" in entries[0]["text"]


def test_split_schema_text_into_entries_table_family_name_is_the_bare_pattern_todays_actual_bigquery_shape():
    # Bug 3 (see backends/bigquery.py's get_schema() shard-family heading
    # comment): fixing bugs 1 and 2 STILL wasn't enough for a user's
    # complaint that the Wikipedia pageviews_* family's name "still
    # appears long and ugly" - because until this fix, get_schema() itself
    # put the fully-qualified, backtick-quoted wildcard right after
    # "Table family:" (the two tests above), unlike every other dialect's
    # shard-family heading (see e.g. test_postgres_backend.py's "Table
    # family: events_<date>") and unlike this SAME dialect's own plain
    # "Table: <table_name>" heading, both of which use a bare, unqualified
    # name. get_schema() now puts the bare pattern there instead - this is
    # the actual shape it emits today. The fully-qualified wildcard BigQuery
    # genuinely needs for querying is still given, verbatim, inside the
    # parenthetical - nothing about the LLM-facing SQL-generation guidance
    # changed, only the leading display token this test's "name" assertion
    # is really about.
    schema = (
        "Table family: pageviews_* "
        "(12 date-sharded tables, e.g. pageviews_2015 .. pageviews_2026; "
        "identical columns in every member - query this family with the "
        "wildcard form `bigquery-public-data.wikipedia.pageviews_*`, "
        "filtering/identifying the shard via the _TABLE_SUFFIX "
        "pseudo-column (e.g. WHERE _TABLE_SUFFIX BETWEEN '...' AND "
        "'...'); never query a single literal date-suffixed table name "
        "from this family) [REQUIRES PARTITION FILTER on datehour]\n"
        "  datehour TIMESTAMP NOT NULL\n"
    )
    entries = split_schema_text_into_entries(schema)
    assert len(entries) == 1
    assert entries[0]["name"] == "pageviews_*"
    assert "`bigquery-public-data.wikipedia.pageviews_*`" in entries[0]["text"]
    assert "[REQUIRES PARTITION FILTER on datehour]" in entries[0]["heading"]


def test_split_schema_text_into_entries_with_no_recognizable_heading_returns_one_name_none_entry():
    schema = "No schema description available."
    entries = split_schema_text_into_entries(schema)
    assert entries == [{"name": None, "heading": None, "text": schema}]


def test_split_schema_text_into_entries_empty_schema_text_returns_empty_list():
    assert split_schema_text_into_entries("") == []
    assert split_schema_text_into_entries(None) == []


# --- derive_tables_only_schema_text() -----------------------------------------
# Backs the SCHEMA_TABLES_ONLY-gated "tables_only" schema kind (see
# translate_routes.py's get_llm_schema_text): every table/table-family/tab
# entry kept with its full per-table detail intact, every OTHER top-level
# schema-object section (Constraints, Indexes, Views, Grants, ...) dropped.
# Deliberately NOT built on top of split_schema_text_into_entries - that
# function's LAST entry runs all the way to len(schema_text), so it would
# swallow every trailing non-table section into whichever table happened to
# be listed last; these tests lock in the different, boundary-search
# strategy that avoids that bug instead.

def test_derive_tables_only_keeps_all_tables_and_drops_everything_after_the_first_non_table_section():
    schema = (
        "Table: customers\n"
        "  id integer NOT NULL\n"
        "  name text NOT NULL\n"
        "\n"
        "Table: orders\n"
        "  id integer NOT NULL\n"
        "  customer_id integer NOT NULL\n"
        "\n"
        "Constraints:\n"
        "  orders.customer_id -> customers.id\n"
        "\n"
        "Indexes:\n"
        "  orders_customer_id_idx on orders(customer_id)\n"
        "\n"
        "Views:\n"
        "  Table: active_customers\n"
        "\n"
        "Likely relationships (naming convention, unconfirmed):\n"
        "  orders.customer_id -> customers.id\n"
    )
    result = derive_tables_only_schema_text(schema)
    assert result == (
        "Table: customers\n"
        "  id integer NOT NULL\n"
        "  name text NOT NULL\n"
        "\n"
        "Table: orders\n"
        "  id integer NOT NULL\n"
        "  customer_id integer NOT NULL"
    )
    assert "Constraints:" not in result
    assert "Indexes:" not in result
    assert "Views:" not in result
    assert "Likely relationships" not in result
    # The nested "Table: active_customers" line inside the Views section
    # must not fool this into thinking the Views section is itself a table
    # entry - it's excluded because it comes after the real boundary, not
    # because of anything special about that one line.


def test_derive_tables_only_keeps_the_overflow_notice_since_its_about_the_tables_not_a_different_object():
    schema = (
        "Table: t1\n"
        "  id integer NOT NULL\n"
        "\n"
        "Table: t2\n"
        "  id integer NOT NULL\n"
        "\n"
        "[... 198 more table(s) not shown - schema truncated ...]\n"
        "\n"
        "Constraints:\n"
        "  t2.id -> t1.id\n"
    )
    result = derive_tables_only_schema_text(schema)
    assert result.endswith("[... 198 more table(s) not shown - schema truncated ...]")
    assert "Constraints:" not in result


def test_derive_tables_only_handles_a_table_family_heading_with_a_nested_parenthetical():
    schema = (
        "Table family: events_<date> "
        "(12 date-sharded tables, e.g. events_20260101 .. events_20261231; "
        "query via UNION ALL across the ones you need)\n"
        "  id integer NOT NULL\n"
        "  occurred_at timestamp NOT NULL\n"
        "\n"
        "Row count estimates:\n"
        "  events_<date>: ~1000000 rows (estimate)\n"
    )
    result = derive_tables_only_schema_text(schema)
    assert result == (
        "Table family: events_<date> "
        "(12 date-sharded tables, e.g. events_20260101 .. events_20261231; "
        "query via UNION ALL across the ones you need)\n"
        "  id integer NOT NULL\n"
        "  occurred_at timestamp NOT NULL"
    )
    assert "Row count estimates" not in result


def test_derive_tables_only_is_a_no_op_when_there_is_no_non_table_section_at_all():
    # Mirrors mongodb_sql.py/sheets.py-shaped schemas, which never emit any
    # of the non-table sections other backends do - nothing to cut, so the
    # full (rstripped) text comes back unchanged.
    schema = (
        "Table: users\n"
        "  _id ObjectId NOT NULL\n"
        "  email string NOT NULL\n"
        "\n"
        "Table: sessions\n"
        "  _id ObjectId NOT NULL\n"
    )
    assert derive_tables_only_schema_text(schema) == schema.rstrip()


def test_derive_tables_only_returns_a_single_table_entry_unchanged():
    schema = "Table: customers\n  id integer NOT NULL\n"
    assert derive_tables_only_schema_text(schema) == schema.rstrip()


def test_derive_tables_only_returns_empty_or_none_input_unchanged():
    assert derive_tables_only_schema_text("") == ""
    assert derive_tables_only_schema_text(None) is None


def test_derive_tables_only_treats_unrecognized_text_with_no_table_heading_as_all_non_table():
    # The "No schema description available." failure placeholder (and any
    # future backend that abandons the heading convention) has no
    # recognizable table heading at all, so its very first line already
    # counts as the "first non-table top-level line" - everything is
    # dropped, matching split_schema_text_into_entries' own treatment of
    # this shape as "nothing structured", not an error.
    schema = "No schema description available."
    assert derive_tables_only_schema_text(schema) == ""
