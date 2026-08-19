"""
Pure-function tests for backends/base.py's schema-size-limiting helpers:
group_date_sharded_tables, cap_kept_tables, cap_schema_text. No app/Flask
involvement needed - these are dependency-free over plain data.
"""

import sys

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.base import group_date_sharded_tables, cap_kept_tables, cap_schema_text


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
