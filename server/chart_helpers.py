"""
chart_helpers.py

Chart/visualization eligibility and validation helpers, extracted out of
translate_routes.py (see that module's own docstring for what's left there
and llm_providers.py's docstring for the first piece extracted the same
way). Nothing here is new behavior, just a change of address.

These four names are a single, self-contained concern: deciding, from the
REAL executed query results (never from the model's own say-so), whether a
single-connection summarization turn is even eligible to be charted at all
(_pick_chartable_result, using _column_looks_numeric and _CHART_MIN_ROWS),
describing that eligibility to the model in its prompt
(_describe_chartable_columns), and validating the model's own chart choice
against the real data afterward (_clean_visualization, also using
_column_looks_numeric). See _pick_chartable_result's docstring for the full
"never trust the model's own judgment about eligibility" rationale that ties
all four together.

Deliberately NOT included here: _build_single_summary_prompt (calls
_describe_chartable_columns, but is otherwise about rendering the whole
single-connection summarization prompt, not charting specifically) and
_clean_single_summary_response (calls _clean_visualization, but is about
parsing/validating that call's whole JSON envelope, "summary" included, not
just the "visualization" piece of it) - both remain in translate_routes.py
as part of the single-connection summarization pipeline, a separate
extraction step of its own.

translate_routes.py re-imports every name below back into its own
namespace, so they remain reachable as translate_routes.<name> - in
particular for every existing test's `app_env.translate_routes.<name>` /
`env.translate_routes.<name>` attribute access (_CHART_MIN_ROWS,
_pick_chartable_result, _clean_visualization, _column_looks_numeric are all
referenced this way in tests/server/test_translate_routes.py). No other
module imports any of these names directly today.

Entirely self-contained: pure functions over plain dicts/lists, no I/O, no
LLM calls, and no imports of their own beyond what's already in scope from
Python's builtins.
"""


_CHART_MIN_ROWS = 2


def _column_looks_numeric(rows, column, sample_size=200):
    """True when a solid majority of `column`'s own non-null sampled values
    (across up to `sample_size` of `rows`) are real numbers. Excludes bool
    (a Python bool is technically an int subclass, but a true/false column
    is categorical, not something to plot on a value axis) and excludes
    numeric-LOOKING strings on purpose - this app never asks the client to
    stringify numbers before sending results here (see
    _build_single_summary_prompt's docstring on `statement_results`' own
    shape: real JSON values, not pre-stringified), so a string value here
    is real text, not a number rendered as text.

    Used twice: to build the prompt's own "Chartable columns" hints (see
    _describe_chartable_columns) and, independently, to re-validate the
    model's actual y_columns choice against the real data rather than
    trusting its guess from the column name alone (e.g. a column named
    "id" is numeric but rarely a sensible y-axis choice on its own - still
    allowed here, since "sensible" is a judgment call left to the model,
    but a column named "amount" that's actually stored as text is not a
    valid choice at all, and this catches that).

    Empty/all-null sampled data is treated as NOT numeric (there's nothing
    to plot), not as a vacuous pass. A solid-majority (not unanimous)
    threshold tolerates the occasional stray null/outlier without
    disqualifying an otherwise-numeric column."""
    seen = 0
    numeric = 0
    for row in (rows or [])[:sample_size]:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None:
            continue
        seen += 1
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric += 1
    if seen == 0:
        return False
    return (numeric / seen) >= 0.9


def _pick_chartable_result(statement_results):
    """Returns the single statement_results entry (see _build_single_
    summary_prompt's own docstring for the shape) eligible to be charted
    this turn, or None when charting isn't offered at all. Deliberately
    conservative - this decides eligibility server-side from the real
    executed results, rather than leaving it to the model's own judgment:
      - Exactly ONE statement_results entry must have real tabular
        columns/rows (not a note, not an error) - a multi-statement script
        with more than one real result set is ambiguous about which one
        to chart, so charting is skipped entirely rather than guessing.
      - That entry must have at least _CHART_MIN_ROWS rows - a single-row
        result has nothing to compare/trend, so a chart adds nothing over
        a table.
      - At least one of its columns must look numeric (_column_looks_
        numeric) - with no numeric column at all there is nothing to plot
        on a value axis.
    Returns the qualifying entry itself (not just True/False) so callers
    have its real columns/rows on hand both for building the prompt's own
    "Chartable columns" list and for later validating the model's column
    choices against them (see _clean_single_summary_response)."""
    tabular = [
        entry for entry in (statement_results or [])
        if isinstance(entry, dict) and not entry.get("error") and not entry.get("note") and entry.get("columns")
    ]
    if len(tabular) != 1:
        return None
    entry = tabular[0]
    rows = entry.get("rows") or []
    if len(rows) < _CHART_MIN_ROWS:
        return None
    columns = entry.get("columns") or []
    if not any(_column_looks_numeric(rows, col) for col in columns):
        return None
    return entry


def _describe_chartable_columns(chartable_entry):
    """Renders `chartable_entry`'s own columns (see _pick_chartable_result)
    into the "Chartable columns" prompt section _SINGLE_SUMMARY_SYSTEM_
    INSTRUCTION's "visualization" paragraph references - each column
    tagged (numeric) or (text) via _column_looks_numeric, so the model can
    tell which columns are even eligible for x_column/y_columns/
    series_column without having to infer types from a raw data dump
    itself. None (nothing chartable this turn - see _pick_chartable_
    result) renders the explicit "no chartable columns" sentence instead,
    so the prompt never leaves the model to guess why "visualization" must
    be null."""
    if chartable_entry is None:
        return "Chartable columns: none available for this turn - \"visualization\" MUST be null.\n"
    rows = chartable_entry.get("rows") or []
    columns = chartable_entry.get("columns") or []
    described = ", ".join(
        f"{col} ({'numeric' if _column_looks_numeric(rows, col) else 'text'})" for col in columns
    )
    return f"Chartable columns (use these EXACT names only): {described}\n"


def _clean_visualization(raw, chartable_entry):
    """Validates the model's own "visualization" value (see
    _SINGLE_SUMMARY_SYSTEM_INSTRUCTION's own paragraph on it) against the
    REAL executed result this turn - `chartable_entry`, the exact same
    _pick_chartable_result(...) value _describe_chartable_columns rendered
    into the prompt the model actually saw. Returns a cleaned
      {"chart_type": "bar"|"line"|"scatter", "x_column": <str>,
       "y_columns": [<str>, ...], "series_column": <str>|None}
    or None (meaning: show a table, not a chart) - never raises, and a
    None return here is never treated as a parse failure by
    _clean_single_summary_response (unlike a genuinely malformed
    "summary") since a table is always an acceptable, valid outcome.

    `chartable_entry` being None (charting wasn't even offered this turn -
    see _pick_chartable_result) forces None regardless of what `raw` says,
    the same "never trust the model's own judgment about eligibility"
    posture _pick_chartable_result's own docstring describes - the model
    was told there were no chartable columns, so anything else it might
    have written for "visualization" anyway is simply ignored, not treated
    as a reason to fail the whole response.

    Otherwise: `raw` must be a dict; "chart_type" must be one of the three
    values the prompt actually offers (deliberately NOT "pie" - see this
    feature's own design notes on why that was left out); "x_column" must
    name one of `chartable_entry`'s real columns; "y_columns" must be a
    non-empty list of real column names, each independently re-verified
    NUMERIC via _column_looks_numeric against the real row data (not just
    "a real column name" - the model was already told which columns are
    numeric, but its choice is re-checked here rather than trusted, same
    as every other LLM output this app validates before acting on it) and
    deduplicated, excluding x_column itself; a "series_column" is kept
    only when it's also a real column, distinct from x_column. Any
    structural problem with "x_column" or an empty "y_columns" after
    filtering invalidates the whole visualization (returns None, falls
    back to table) rather than partially rendering something the model
    didn't actually intend."""
    if chartable_entry is None or not isinstance(raw, dict):
        return None
    chart_type = raw.get("chart_type")
    if chart_type not in ("bar", "line", "scatter"):
        return None
    columns = chartable_entry.get("columns") or []
    rows = chartable_entry.get("rows") or []
    column_set = set(columns)

    x_column = raw.get("x_column")
    if not (isinstance(x_column, str) and x_column in column_set):
        return None

    raw_y_columns = raw.get("y_columns")
    if not isinstance(raw_y_columns, list):
        return None
    y_columns = []
    for y in raw_y_columns:
        if (
            isinstance(y, str) and y in column_set and y != x_column
            and y not in y_columns and _column_looks_numeric(rows, y)
        ):
            y_columns.append(y)
    if not y_columns:
        return None

    series_column = raw.get("series_column")
    if not (isinstance(series_column, str) and series_column in column_set and series_column != x_column):
        series_column = None

    return {
        "chart_type": chart_type, "x_column": x_column,
        "y_columns": y_columns, "series_column": series_column,
    }
